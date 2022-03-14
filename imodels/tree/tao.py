import random
from copy import deepcopy

import numpy as np
from mlxtend.classifier import LogisticRegression
from sklearn import datasets
from sklearn.base import BaseEstimator
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
from sklearn.utils import check_X_y


class TaoTree(BaseEstimator):
    """
    """

    def __init__(self, model_type: str = 'CART',
                 reg_param: float = 1e-3,
                 n_iters: int = 20,
                 model_args: dict = {'max_leaf_nodes': 15},
                 randomize_tree=False,
                 min_node_samples_tao=3,
                 min_leaf_samples_tao=2,
                 node_model='stump',
                 weight_errors: bool = True,
                 ):
        """TAO: Alternating optimization of decision trees, with application to learning sparse oblique trees (Neurips 2018)
        https://proceedings.neurips.cc/paper/2018/hash/185c29dc24325934ee377cfda20e414c-Abstract.html
        Note: this implementation learns single-feature splits rather than oblique trees.

        Currently supports
        - given a CART tree, posthoc improve it with TAO

        Requirements
        - given a FIGS model, posthoc improve it
        - weight binary errors more carefully
        - learn a new model (changing the classifier at any given node) - this requires a new data structure
        - support pruning (e.g. if weights -> 0, then remove a node)
        - support classifiers in leaves

        Parameters
        ----------

        model_type: str
            'CART' or 'FIGS'

        reg_param
            Regularization parameter for node-wise linear model

        n_iters
            Number of iterations to run TAO

        model_args
            Arguments to pass to the model

        randomize_tree
            Whether to randomize the tree before each iteration

        min_node_samples_tao: int
            Minimum number of samples in a node to apply tao

        min_leaf_samples_tao: int

        node_model: str
            'stump' or 'linear'
        """
        super().__init__()
        self.model_type = model_type
        self.reg_param = reg_param
        self.n_iters = n_iters
        self.model_args = model_args
        self.randomize_tree = randomize_tree
        self.min_node_samples_tao = min_node_samples_tao
        self.min_leaf_samples_tao = min_leaf_samples_tao
        self.node_model = node_model
        self.weight_errors = weight_errors
        self._init_prediction_task()  # decides between regressor and classifier

    def _init_prediction_task(self):
        """
        TaoRegressor and TaoClassifier override this method
        to alter the prediction task. When using this class directly,
        it is equivalent to SuperCARTRegressor
        """
        self.prediction_task = 'classification'

    def fit(self, X, y=None, feature_names=None):
        """
        Params
        ------
        _sample_weight: array-like of shape (n_samples,), default=None
            Sample weights. If None, then samples are equally weighted.
            Splits that would create child nodes with net zero or negative weight
            are ignored while searching for a split in each node.
        """

        if self.prediction_task == 'classification':
            self.classes_, y = np.unique(y, return_inverse=True)  # deals with str inputs
        X, y = check_X_y(X, y)
        y = y.astype(float)
        if feature_names is not None:
            self.feature_names_ = feature_names
        if self.model_type == 'CART':
            if self.prediction_task == 'classification':
                self.model = DecisionTreeClassifier(**self.model_args)
            elif self.prediction_task == 'regression':
                self.model = DecisionTreeRegressor(**self.model_args)
            self.model.fit(X, y)
            # print(export_text(self.model))
            # plot_tree(self.model)
            # plt.show()

        if self.randomize_tree:
            np.random.shuffle(self.model.tree_.feature)  # shuffle CART features
            # np.random.shuffle(self.model.tree_.threshold)
            for i in range(self.model.tree_.node_count):  # split on feature medians
                self.model.tree_.threshold[i] = np.median(
                    X[:, self.model.tree_.feature[i]])

        for i in range(self.n_iters):
            num_updates = self._tao_iter_cart(X, y, self.model.tree_)
            if num_updates == 0:
                break

        return self

    def _tao_iter_cart(self, X, y, tree):
        """Updates tree by applying the tao algorithm to the tree
        Params
        ------
        X: array-like of shape (n_samples, n_features)
            The input samples.
        y: array-like of shape (n_samples,)
            The target values.
        model: DecisionTreeClassifier.tree_ or DecisionTreeRegressor.tree_
            The model to be post-hoc improved
        """

        # Tree properties
        children_left = tree.children_left
        children_right = tree.children_right
        feature = tree.feature
        threshold = tree.threshold
        value = tree.value

        # For each node, compute the path to that node
        indexes_with_prefix_paths = []  # data structure with (index, path_to_node_index)
        # e.g. if if node 3 is the left child of node 1 which is the right child of node 0
        # then we get (3, [(0, R), (1, L)])
        stack = [(0, [])]  # start with the root node id (0) and its depth (0)
        while len(stack) > 0:
            node_id, path_to_node_index = stack.pop()
            indexes_with_prefix_paths.append((node_id, path_to_node_index))

            # If a split node, append left and right children and depth to `stack`
            if children_left[node_id] != children_right[node_id]:
                stack.append((children_left[node_id], path_to_node_index + [(node_id, 'L')]))
                stack.append((children_right[node_id], path_to_node_index + [(node_id, 'R')]))
        # print(indexes_with_prefix_paths)

        # Iterate through each node and compute the path to the leaf node
        num_updates = 0
        for (node_id, path_to_node_index) in indexes_with_prefix_paths:
            # print('node_id', node_id, path_to_node_index)

            # Compute the points being input to the node
            def filter_points_by_path(X, y, path_to_node_index):
                """
                Returns the points in X that are in the path to the node
                """
                for node_id, direction in path_to_node_index:
                    idxs = X[:, feature[node_id]] <= threshold[node_id]
                    if direction == 'R':
                        idxs = ~idxs
                    # print('idxs', idxs.size, idxs.sum())
                    X = X[idxs]
                    y = y[idxs]
                return X, y

            X_node, y_node = filter_points_by_path(X, y, path_to_node_index)
            if children_left[node_id] == children_right[node_id]:  # is leaf node
                if self.prediction_task == 'regression' and X_node.shape[0] >= self.min_leaf_samples_tao:
                    # old_score = self.model.score(X, y)
                    value[node_id] = np.mean(y_node)
                    """
                    new_score = self.model.score(X, y)
                    if new_score > old_score:
                        print(f'\tLeaf improved score from {old_score:0.3f} to {new_score:0.3f}')
                    if new_score < old_score:
                        print(f'\tLeaf reduced score from {old_score:0.3f} to {new_score:0.3f}')
                        # raise ValueError('Leaf update reduced score')
                    """
                # print('\tshapes', X_node.shape, y_node.shape)
                # print('\tvals:', value[node_id][0][0], np.mean(y_node))
                # assert value[node_id][0][0] == np.mean(y_node), 'unless tree changed, vals should be leaf means'
                continue
            elif X_node.shape[0] < self.min_node_samples_tao:
                continue

            # Compute the outputs for these points if they go left or right
            def predict_from_node(X, node_id):
                """Returns predictions for X starting at node node_id"""

                def predict_from_node(x, node_id):
                    """Returns predictions for x starting at node node_id"""
                    if children_left[node_id] == children_right[node_id]:
                        if self.prediction_task == 'regression':
                            return value[node_id]
                        elif self.prediction_task == 'classification':
                            return value[node_id][0, 1]
                    if x[feature[node_id]] <= threshold[node_id]:
                        return predict_from_node(x, children_left[node_id])
                    else:
                        return predict_from_node(x, children_right[node_id])

                preds = np.zeros(X.shape[0])
                for i in range(X.shape[0]):
                    preds[i] = predict_from_node(X[i], node_id)
                return preds

            y_node_left = predict_from_node(X_node, children_left[node_id])
            y_node_right = predict_from_node(X_node, children_right[node_id])

            # Decide on prediction target (want to go left (0) / right (1) when advantageou
            # the TAO paper binarize these (e.g. predict 0 or 1 depending on which of these is correct)
            # here, we weight these errors for regression
            # if self.prediction_task == 'regression':
            y_node_absolute_errors = np.abs(np.vstack((y_node - y_node_left, y_node - y_node_right))).T

            # could screen out where this is 0 to make it faster (for classification)
            y_node_target = np.argmin(y_node_absolute_errors, axis=1)

            # weight by the difference in error
            if self.weight_errors:
                sample_weight = np.abs(y_node_absolute_errors[:, 1], y_node_absolute_errors[:, 0])
            else:
                sample_weight = np.ones(y_node.size)

            # Fit a 1-variable binary classification model on these outputs
            # Note: this could be customized (e.g. for sparse oblique trees)
            best_score = -np.inf
            best_feat = None
            best_model = None
            for feat_num in range(X.shape[1]):
                if self.prediction_task == 'classification':
                    if self.node_model == 'linear':
                        m = LogisticRegression()
                    elif self.node_model == 'stump':
                        m = DecisionTreeClassifier(max_depth=1)
                elif self.prediction_task == 'regression':
                    if self.node_model == 'linear':
                        m = LinearRegression()
                    elif self.node_model == 'stump':
                        m = DecisionTreeRegressor(max_depth=1)
                m.fit(X_node[:, feat_num: feat_num + 1], y_node_target, sample_weight=sample_weight)
                score = m.score(X_node[:, feat_num: feat_num + 1], y_node_target, sample_weight=sample_weight)
                if score > best_score:
                    best_score = score
                    best_feat = feat_num
                    best_model = deepcopy(m)
            if self.node_model == 'linear':
                best_threshold = -best_model.intercept_ / best_model.coef_[0]
            elif self.node_model == 'stump':
                best_threshold = m.tree_.feature[0]
            # print((feature[node_id], threshold[node_id]), '\n->',
            #       (best_feat, best_threshold))

            # Update the node with the new feature / threshold
            old_feat = feature[node_id]
            old_threshold = threshold[node_id]
            # print(X.sum(), y.sum())
            old_score = self.model.score(X, y)

            feature[node_id] = best_feat
            threshold[node_id] = best_threshold
            new_score = self.model.score(X, y)
            # print(f'\tscore from {old_score:0.3f} to {new_score:0.3f}')
            if old_score >= new_score:
                feature[node_id] = old_feat
                threshold[node_id] = old_threshold
            else:
                # (Track if any updates were necessary)
                num_updates += 1
                # print(f'Improved score from {old_score:0.3f} to {new_score:0.3f}')

        return num_updates

    def predict(self, X):
        return self.model.predict(X)

    def predict_proba(self, X):
        return self.model.predict_proba(X)


class TaoTreeRegressor(TaoTree):
    def _init_prediction_task(self):
        self.prediction_task = 'regression'


class TaoTreeClassifier(TaoTree):
    def _init_prediction_task(self):
        self.prediction_task = 'classification'


if __name__ == '__main__':
    np.random.seed(13)
    random.seed(13)
    X, y = datasets.load_breast_cancer(return_X_y=True)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.33, random_state=42
    )
    print('X.shape', X.shape)
    print('ys', np.unique(y_train), '\n\n')
    m = TaoClassifier()
    m.fit(X_train, y_train)
    print('acc', np.mean(m.predict(X_test) == y_test))
    # print(m.predict(X_train), m.predict_proba(X_train).shape)
    # print(m.predict_proba(X_train))

    # X, y = datasets.load_diabetes(return_X_y=True)  # regression
    # X = np.random.randn(500, 10)
    # y = (X[:, 0] > 0).astype(float) + (X[:, 1] > 1).astype(float)
    # X_train, X_test, y_train, y_test = train_test_split(
    #     X, y, test_size=0.33, random_state=42
    # )
    # m = TaoRegressor()
    # m.fit(X_train, y_train)
    # print('mse', np.mean(np.square(m.predict(X_test) - y_test)),
    #       'baseline', np.mean(np.square(y_test)))