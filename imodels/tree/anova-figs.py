from copy import deepcopy
from typing import List

import numpy as np
import pandas as pd
import sklearn.datasets
from sklearn import datasets
from sklearn import tree
from sklearn.base import BaseEstimator
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.utils import check_X_y, check_array

from itertools import chain, combinations

from figs import FIGS, FIGSCV #, Node


class Node:
    def __init__(self, feature: int = None, threshold: int = None,
                 value=None, idxs=None, is_root: bool = False, left=None,
                 impurity_reduction: float = None, tree_num: int = None,
                 right=None):
        """Node class for splitting
        """

        # split or linear
        self.is_root = is_root
        self.idxs = idxs
        self.tree_num = tree_num
        self.feature = feature
        self.impurity_reduction = impurity_reduction

        # different meanings
        self.value = value  # for split this is mean, for linear this is weight

        # split-specific
        self.threshold = threshold
        self.left = left
        self.right = right
        self.left_temp = None
        self.right_temp = None

    def setattrs(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __str__(self):
        if self.is_root:
            return f'X_{self.feature} <= {self.threshold:0.3f} (Tree #{self.tree_num} root)'
        elif self.left is None and self.right is None:
            best_class_idx = np.where(self.value == np.max(self.value))
            return f'Val: {np.array([idx[0] for idx in best_class_idx])} (leaf)'
        else:
            return f'X_{self.feature} <= {self.threshold:0.3f} (split)'

    def __repr__(self):
        return self.__str__()


class AFIGS(FIGS):
    """FIGS (sum of trees) classifier.
    Fast Interpretable Greedy-Tree Sums (FIGS) is an algorithm for fitting concise rule-based models.
    Specifically, FIGS generalizes CART to simultaneously grow a flexible number of trees in a summation.
    The total number of splits across all the trees can be restricted by a pre-specified threshold, keeping the model interpretable.
    Experiments across real-world datasets show that FIGS achieves state-of-the-art prediction performance when restricted to just a few splits (e.g. less than 20).
    https://arxiv.org/abs/2201.11931
    """

    def __init__(self, max_int: int = None, max_rules: int = 12, min_impurity_decrease: float = 0.0):
        super().__init__()
        self.max_int = max_int
        self.max_rules = max_rules
        self.min_impurity_decrease = min_impurity_decrease
        self.prediction_task = 'classification'
        # self._init_prediction_task()  # decides between regressor and classifier
        self._init_decision_function()
        self.y_levels = None
        self.n = None

    def _init_y(self, y):
        self.y_levels = list(np.max(y, axis=0).astype(int) + 1)
        self.n = y.shape[0]

        # create one-hot encoded y array
        y_onehot = np.zeros([self.n] + self.y_levels)
        y_onehot_idx = np.concatenate((np.arange(self.n).reshape((self.n, 1)), y), axis=1).astype(int)
        for i in range(self.n):
            y_onehot[tuple(y_onehot_idx[i, :])] = 1

        # flatten one-hot encoded y array for input into the decision trees
        y_cols = list(range(y.shape[1]))
        if self.max_int is None:
            self.max_int = len(y_cols)
        elif self.max_int > len(y_cols):
            self.max_int = len(y_cols)
        y_dims = list(chain.from_iterable(combinations(y_cols, r) for r in range(1, self.max_int+1)))
        y_dict = {}
        for i, dims in enumerate(y_dims):
            y_dict[dims] = self._flatten_y(y_onehot, dims)

        return y_dict, y_onehot

    def _flatten_y(self, y_onehot, dims):
        y_agg = np.sum(
            y_onehot, axis=tuple(i+1 for i in range(len(self.y_levels)) if i not in dims)
        )
        if y_agg.ndim > 2:
            y_flat = np.zeros((self.n, y_agg[0, ...].size))
            for i in range(self.n):
                y_flat[i, ...] = y_agg[i, ...].flatten()
        else:
            y_flat = deepcopy(y_agg)

        return y_flat

    def _compute_node_value(self, y_all, idxs):
        val_ids, val_counts = np.unique(y_all[idxs, :], axis=0, return_counts=True)
        value = np.zeros(self.y_levels)
        for i in range(val_ids.shape[0]):
            value[tuple(val_ids[i, :].astype(int))] = val_counts[i] / idxs.sum()
        return value

    def _construct_node_with_stump(self, X, y, y_all, idxs, tree_num, sample_weight=None):
        # array indices
        SPLIT = 0
        LEFT = 1
        RIGHT = 2

        # fit stump
        stump = tree.DecisionTreeRegressor(max_depth=1)
        if sample_weight is not None:
            sample_weight = sample_weight[idxs]
        stump.fit(X[idxs], y[idxs], sample_weight=sample_weight)

        # these are all arrays, arr[0] is split node
        # note: -2 is dummy
        feature = stump.tree_.feature
        threshold = stump.tree_.threshold

        impurity = stump.tree_.impurity
        n_node_samples = stump.tree_.n_node_samples
        value = self._compute_node_value(y_all, idxs)

        # no split
        if len(feature) == 1:
            # print('no split found!', idxs.sum(), impurity, feature)
            return Node(idxs=idxs, value=value, tree_num=tree_num,
                        feature=feature[SPLIT], threshold=threshold[SPLIT],
                        impurity_reduction=None)

        # split node
        impurity_reduction = (
                                     impurity[SPLIT] -
                                     impurity[LEFT] * n_node_samples[LEFT] / n_node_samples[SPLIT] -
                                     impurity[RIGHT] * n_node_samples[RIGHT] / n_node_samples[SPLIT]
                             ) * idxs.sum()

        node_split = Node(idxs=idxs, value=value, tree_num=tree_num,
                          feature=feature[SPLIT], threshold=threshold[SPLIT],
                          impurity_reduction=impurity_reduction)
        # print('\t>>>', node_split, 'impurity', impurity, 'num_pts', idxs.sum(), 'imp_reduc', impurity_reduction)

        # manage children
        idxs_split = X[:, feature[SPLIT]] <= threshold[SPLIT]
        idxs_left = idxs_split & idxs
        idxs_right = ~idxs_split & idxs
        value_left = self._compute_node_value(y_all, idxs_left)
        value_right = self._compute_node_value(y_all, idxs_right)
        node_left = Node(idxs=idxs_left, value=value_left, tree_num=tree_num)
        node_right = Node(idxs=idxs_right, value=value_right, tree_num=tree_num)
        node_split.setattrs(left_temp=node_left, right_temp=node_right, )
        return node_split

    def fit(self, X, y=None, feature_names=None, verbose=False, sample_weight=None):
        """
        Params
        ------
        _sample_weight: array-like of shape (n_samples,), default=None
            Sample weights. If None, then samples are equally weighted.
            Splits that would create child nodes with net zero or negative weight
            are ignored while searching for a split in each node.
        """
        # X, y = check_X_y(X, y)
        y = y.astype(float)
        if feature_names is not None:
            self.feature_names_ = feature_names

        self.complexity_ = 0  # tracks the number of rules in the model
        y_predictions_per_tree = {}  # predictions for each tree
        y_residuals_per_tree = {}  # based on predictions above

        # reformat response data y; one (key, value) pair for each y response/tree
        y_dict, y_onehot = self._init_y(y)
        self.trees_ = [None for i in range(len(y_dict))]  # list of the root nodes of added trees

        # set up initial potential_splits - one for each tree
        # everything in potential_splits either is_root (so it can be added directly to self.trees_)
        # or it is a child of a root node that has already been added
        idxs = np.ones(self.n, dtype=bool)
        potential_splits = [];
        for tree_num_, (tree_id, y_resp) in enumerate(y_dict.items()):
            node_init = self._construct_node_with_stump(X=X, y=y_resp, y_all=y, idxs=idxs, tree_num=tree_num_, sample_weight=sample_weight)
            # node_init = self._construct_node_with_stump(X=X, y=y, idxs=idxs, tree_num=-1, sample_weight=sample_weight)
            potential_splits.append(node_init)
        for node in potential_splits:
            node.setattrs(is_root=True)
        potential_splits = sorted(potential_splits, key=lambda x: x.impurity_reduction)

        # start the greedy fitting algorithm
        finished = False
        while len(potential_splits) > 0 and not finished:
            # print('potential_splits', [str(s) for s in potential_splits])
            split_node = potential_splits.pop()  # get node with max impurity_reduction (since it's sorted)

            # don't split on node
            if split_node.impurity_reduction < self.min_impurity_decrease:
                finished = True
                break

            # split on node
            if verbose:
                print('\nadding ' + str(split_node))
            self.complexity_ += 1

            # if added a tree root
            if split_node.is_root:

                # start a new tree
                self.trees_[split_node.tree_num] = split_node

                # update tree_num
                # for node_ in [split_node, split_node.left_temp, split_node.right_temp]:
                #     if node_ is not None:
                #         node_.tree_num = len(self.trees_) - 1

                # add new root potential node
                # node_new_root = Node(is_root=True, idxs=np.ones(X.shape[0], dtype=bool),
                #                      tree_num=-1)
                # potential_splits.append(node_new_root)

            # add children to potential splits
            # assign left_temp, right_temp to be proper children
            # (basically adds them to tree in predict method)
            split_node.setattrs(left=split_node.left_temp, right=split_node.right_temp)

            # add children to potential_splits
            potential_splits.append(split_node.left)
            potential_splits.append(split_node.right)

            # update predictions for altered tree
            for tree_num_ in range(len(self.trees_)):
                if self.trees_[tree_num_] is None:
                    y_predictions_per_tree[tree_num_] = np.zeros([self.n] + self.y_levels)
                else:
                    y_predictions_per_tree[tree_num_] = self._predict_tree(self.trees_[tree_num_], X)
            # y_predictions_per_tree[-1] = np.zeros(X.shape[0])  # dummy 0 preds for possible new trees

            # update residuals for each tree
            # -1 is key for potential new tree
            for tree_num_, tree_id in enumerate(y_dict.keys()):
                y_residuals_per_tree[tree_num_] = deepcopy(y_onehot)

                for tree_num_other_, tree_id_other in enumerate(y_dict.keys()):
                    if (not tree_num_other_ == tree_num_) & \
                            (len(set(tree_id).intersection(set(tree_id_other))) > 0):
                        y_residuals_per_tree[tree_num_] -= y_predictions_per_tree[tree_num_other_]

            # recompute all impurities + update potential_split children
            potential_splits_new = []
            for potential_split in potential_splits:
                tree_num_ = potential_split.tree_num
                tree_id = list(y_dict.keys())[tree_num_]

                # aggregate predictions based on tree_id
                y_target = self._flatten_y(y_residuals_per_tree[tree_num_], tree_id)

                # re-calculate the best split
                potential_split_updated = self._construct_node_with_stump(X=X,
                                                                          y=y_target,
                                                                          y_all=y,
                                                                          idxs=potential_split.idxs,
                                                                          tree_num=tree_num_,
                                                                          sample_weight=sample_weight, )

                # need to preserve certain attributes from before (value at this split + is_root)
                # value may change because residuals may have changed, but we want it to store the value from before
                potential_split.setattrs(
                    feature=potential_split_updated.feature,
                    threshold=potential_split_updated.threshold,
                    impurity_reduction=potential_split_updated.impurity_reduction,
                    left_temp=potential_split_updated.left_temp,
                    right_temp=potential_split_updated.right_temp,
                )

                # this is a valid split
                if potential_split.impurity_reduction is not None:
                    potential_splits_new.append(potential_split)

            # sort so largest impurity reduction comes last (should probs make this a heap later)
            potential_splits = sorted(potential_splits_new, key=lambda x: x.impurity_reduction)
            if verbose:
                print(self)
            if self.max_rules is not None and self.complexity_ >= self.max_rules:
                finished = True
                break
        return self

    def _predict_tree(self, root: Node, X):
        """Predict for a single tree
                """

        def _predict_tree_single_point(root: Node, x):
            if root.left is None and root.right is None:
                return root.value
            left = x[root.feature] <= root.threshold
            if left:
                if root.left is None:  # we don't actually have to worry about this case
                    return root.value
                else:
                    return _predict_tree_single_point(root.left, x)
            else:
                if root.right is None:  # we don't actually have to worry about this case
                    return root.value
                else:
                    return _predict_tree_single_point(root.right, x)


        preds = np.zeros([X.shape[0]] + self.y_levels)
        for i in range(X.shape[0]):
            preds[i, ...] = _predict_tree_single_point(root, X[i])
        return preds

    def predict(self, X):
        X = check_array(X)
        preds = np.zeros([self.n] + self.y_levels)
        for tree in self.trees_:
            if tree is not None:
                preds += self._predict_tree(tree, X)
        if self.prediction_task == 'regression':
            return NotImplemented
        elif self.prediction_task == 'classification':
            class_preds = np.zeros((self.n, len(self.y_levels)))
            for i in range(self.n):
                best_class_idx = np.where(preds[i, :] == np.max(preds[i, :]))
                class_preds[i, :] = np.array([idx[0] for idx in best_class_idx])
            return class_preds.astype(int)

    def predict_proba(self, X):
        X = check_array(X)
        if self.prediction_task == 'regression':
            return NotImplemented
        preds = np.zeros([self.n] + self.y_levels)
        for tree in self.trees_:
            if tree is not None:
                preds += self._predict_tree(tree, X)
        # preds = np.clip(preds, a_min=0., a_max=1.)  # constrain to range of probabilities
        return preds


class AFIGSRegressor(AFIGS):
    def _init_prediction_task(self):
        # self.prediction_task = 'regression'
        return NotImplemented


class AFIGSClassifier(AFIGS):
    def _init_prediction_task(self):
        self.prediction_task = 'classification'


class AFIGSRegressorCV(FIGSCV):
    def __init__(self,
                 n_rules_list: List[int] = [6, 12, 24, 30, 50],
                 cv: int = 3, scoring='r2', *args, **kwargs):
        # super(AFIGSRegressorCV, self).__init__(figs=AFIGSRegressor, n_rules_list=n_rules_list,
        #                                        cv=cv, scoring=scoring, *args, **kwargs)
        return NotImplemented


class AFIGSClassifierCV(FIGSCV):
    def __init__(self,
                 n_rules_list: List[int] = [6, 12, 24, 30, 50],
                 cv: int = 3, scoring="accuracy", *args, **kwargs):
        super(AFIGSClassifierCV, self).__init__(figs=AFIGSClassifier, n_rules_list=n_rules_list,
                                                cv=cv, scoring=scoring, *args, **kwargs)


if __name__ == '__main__':
    from sklearn import datasets
    from os.path import join as oj
    import random

    random.seed(331)

    # toy example
    X_cls, Y_cls = datasets.load_breast_cancer(return_X_y=True)
    Y_cls = np.column_stack((Y_cls, np.random.permutation(Y_cls)))
    # X_reg, Y_reg = datasets.make_friedman1(100)

    # est = FIGSRegressorCV()
    # est.fit(X_reg, Y_reg)
    # est.predict(X_reg)
    # print(est.max_rules)

    est = AFIGS()
    est.fit(X_cls, Y_cls, verbose=True)
    yhat = est.predict(X_cls)
    yhat_prob = est.predict_proba(X_cls)
    print(sklearn.metrics.confusion_matrix(Y_cls[:, 0], yhat[:, 0]))
    print(sklearn.metrics.confusion_matrix(Y_cls[:, 1], yhat[:, 1]))
    print(est.max_rules)

    # JGI example
    # jgi_dir = oj("..", "..", "JGI", "JGI", "data")
    jgi_dir = oj("..", "..", "..", "JGI", "JGI", "data")
    X = pd.read_csv(oj(jgi_dir, "X_filtered_HILIC_only.csv")).to_numpy()
    # X = X[:, np.random.choice(list(range(X.shape[1])), 1000, replace=False)]
    Y = pd.read_csv(oj(jgi_dir, "Y_numeric_filtered_HILIC_only.csv")).drop(columns=["ecofab"]).to_numpy()

    est = AFIGS(max_int=2, max_rules=24)
    est.fit(X, Y, verbose=True)
    yhat = est.predict(X)
    yhat_prob = est.predict_proba(X)
    for j in range(Y.shape[1]):
        print(sklearn.metrics.confusion_matrix(Y[:, j], yhat[:, j]))
    print(est.max_rules)

#%%