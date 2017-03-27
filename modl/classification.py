import numbers

import mkl
import numpy as np
from lightning.impl.fista import FistaClassifier
from scipy.linalg import lstsq
from sklearn.base import TransformerMixin, BaseEstimator
from sklearn.externals.joblib import Memory
from sklearn.linear_model import LogisticRegressionCV, LogisticRegression
from sklearn.linear_model.base import LinearClassifierMixin
from sklearn.model_selection import GridSearchCV
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import LabelBinarizer
from sklearn.preprocessing import StandardScaler
from sklearn.utils import check_array, gen_batches, \
    check_random_state, check_X_y


class Projector(TransformerMixin, BaseEstimator):
    def __init__(self, basis,
                 n_jobs=1,
                 identity=False):
        self.basis = basis
        self.n_jobs = n_jobs

        self.identity = identity

    def fit(self, X=None, y=None):
        return self

    def transform(self, X):
        current_n_threads = mkl.get_max_threads()
        mkl.set_num_threads(self.n_jobs)
        loadings, _, _, _ = lstsq(self.basis.T, X.T)
        mkl.set_num_threads(current_n_threads)
        loadings = loadings.T
        return loadings

    def inverse_transform(self, Xt):
        rec = Xt.dot(self.basis)
        return rec


class LabelDropper(TransformerMixin, BaseEstimator):
    def fit(self, X=None, y=None):
        return self

    def transform(self, X):
        X = X.drop('dataset', axis=1)
        return X


class LabelGetter(TransformerMixin, BaseEstimator):
    def fit(self, X=None, y=None):
        return self

    def transform(self, X, y=None):
        return X[['dataset']]


def make_loadings_extractor(bases, scale_bases=True,
                            identity=False,
                            standardize=True, scale_importance=True,
                            memory=Memory(cachedir=None),
                            n_jobs=1, ):
    if not isinstance(bases, list):
        bases = [bases]
    if scale_bases:
        for i, basis in enumerate(bases):
            S = np.std(basis, axis=1)
            S[S == 0] = 0
            basis = basis / S[:, np.newaxis]
            bases[i] = basis

    feature_union = []
    for i, basis in enumerate(bases):
        projection_pipeline = Pipeline([('projector',
                                         Projector(basis, n_jobs=n_jobs)),
                                        ('standard_scaler',
                                         StandardScaler(
                                             with_std=standardize))],
                                       memory=memory)
        feature_union.append(('scaled_projector_%i' % i, projection_pipeline))
    if identity:
        feature_union.append(('scaled_identity',
                              StandardScaler(with_std=standardize)))

    # Weighting
    transformer_weights = {}
    sizes = np.array([basis.shape[0] for basis in bases])
    if scale_importance is None:
        scales = np.ones(len(bases))
    elif scale_importance == 'sqrt':
        scales = np.sqrt(sizes)
    elif scale_importance == 'linear':
        scales = sizes
    else:
        raise ValueError
    if identity:
        n_features = bases[0].shape[1]
        scales = np.concatenate([scales, np.array(1. / n_features)])
    for i in range(len(bases)):
        transformer_weights[
            'scaled_projector_%i' % i] = 1. / scales[i] / np.sum(1. / scales)
    if identity:
        transformer_weights[
            'scaled_identity'] = 1. / scales[-1] / np.sum(1. / scales)

    concatenated_projector = FeatureUnion(feature_union,
                                          transformer_weights=
                                          transformer_weights)

    projector = Pipeline([('label_dropper', LabelDropper()),
                          ('concatenated_projector',
                           concatenated_projector)])
    transformer = FeatureUnion([('projector', projector),
                                ('label_getter', LabelGetter())])

    return transformer


def make_classifier(alphas, beta,
                    latent_dim,
                    factored=True,
                    fit_intercept=True,
                    max_iter=10,
                    activation='linear',
                    fine_tune=True,
                    n_jobs=1,
                    penalty='l2',
                    dropout=0,
                    # Useful for non factored LR
                    multi_class='multinomial',
                    tol=1e-4,
                    train_samples=1,
                    random_state=None,
                    verbose=0):
    if not hasattr(alphas, '__iter__'):
        alphas = [alphas]
    if factored:
        classifier = FactoredLogistic(optimizer='adam',
                                      max_iter=max_iter,
                                      activation=activation,
                                      fit_intercept=fit_intercept,
                                      latent_dim=latent_dim,
                                      dropout=dropout,
                                      alpha=alphas[0],
                                      fine_tune=fine_tune,
                                      beta=beta,
                                      batch_size=200,
                                      n_jobs=n_jobs,
                                      verbose=verbose)
        if len(alphas) > 1:
            classifier.set_params(n_jobs=1)
            classifier = GridSearchCV(classifier,
                                      {'alpha': alphas},
                                      cv=10,
                                      refit=True,
                                      verbose=verbose,
                                      n_jobs=n_jobs)
    else:
        if penalty == 'trace':
            multiclass = multi_class == 'multinomial'
            classifier = FistaClassifier(
                multiclass=multiclass == 'multinomial',
                loss='log', penalty='trace',
                max_iter=max_iter,
                alpha=alphas[0],
                verbose=verbose,
                C=1. / train_samples)
            if len(alphas) > 1:
                classifier = GridSearchCV(classifier,
                                          {'alpha': alphas},
                                          cv=10,
                                          refit=True,
                                          verbose=verbose,
                                          n_jobs=n_jobs)
        else:
            if len(alphas) > 1:
                classifier = LogisticRegressionCV(solver='saga',
                                                  multi_class=multi_class,
                                                  fit_intercept=fit_intercept,
                                                  random_state=random_state,
                                                  refit=True,
                                                  tol=tol,
                                                  max_iter=max_iter,
                                                  n_jobs=n_jobs,
                                                  penalty=penalty,
                                                  cv=10,
                                                  verbose=verbose,
                                                  Cs=1. / train_samples / alphas)
            else:
                classifier = LogisticRegression(solver='saga',
                                                multi_class=multi_class,
                                                fit_intercept=fit_intercept,
                                                random_state=random_state,
                                                tol=tol,
                                                max_iter=max_iter,
                                                n_jobs=n_jobs,
                                                penalty=penalty,
                                                verbose=verbose,
                                                C=1. / train_samples / alphas[
                                                    0])
    return classifier


class FactoredLogistic(BaseEstimator, LinearClassifierMixin):
    """
    Parameters
    ----------

    latent_dim: int,

    activation: str, Keras activation

    optimizer: str, Keras optimizer
    """

    def __init__(self, latent_dim=10, activation='linear',
                 optimizer='adam',
                 fit_intercept=True,
                 fine_tune=True,
                 max_iter=100, batch_size=256, n_jobs=1, alpha=0.01,
                 beta=0.01,
                 dropout=0,
                 random_state=None,
                 early_stop=True,
                 verbose=0
                 ):
        self.latent_dim = latent_dim
        self.activation = activation
        self.fine_tune = fine_tune
        self.optimizer = optimizer
        self.max_iter = max_iter
        self.batch_size = batch_size
        self.shuffle = True
        self.alpha = alpha
        self.n_jobs = n_jobs
        self.dropout = dropout
        self.random_state = random_state
        self.fit_intercept = fit_intercept
        self.verbose = verbose
        self.beta = beta
        self.early_stop = early_stop

    def fit(self, X, y, validation_data=None):
        """Fit the model according to the given training data.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            Training vector, where n_samples is the number of samples and
            n_features is the number of features.

        y : array-like, shape (n_samples,)
            Target vector relative to X.

        sample_weight : array-like, shape (n_samples,) optional
            Array of weights that are assigned to individual samples.
            If not provided, then each sample is given unit weight.

            .. versionadded:: 0.17
               *sample_weight* support to LogisticRegression.

        Returns
        -------
        self : object
            Returns self.
        """
        if not isinstance(self.max_iter, numbers.Number) or self.max_iter < 0:
            raise ValueError("Maximum number of iteration must be positive;"
                             " got (max_iter=%r)" % self.max_iter)

        X, y = check_X_y(X, y, accept_sparse='csr', dtype=np.float32,
                         order="C")
        datasets = X[:, -1].astype('int')
        X = X[:, :-1]

        self.random_state = check_random_state(self.random_state)

        if validation_data is not None:
            X_val, y_val = validation_data
            X_val, y_val = check_X_y(X_val, y_val,
                                     accept_sparse='csr', dtype=np.float32,
                                     order="C")
            datasets_val = X_val[:, -1].astype('int')
            X_val = X_val[:, :-1]
            do_validation = True
        else:
            do_validation = False

        n_samples, n_features = X.shape

        self.datasets_ = np.unique(datasets)
        print('datasets', self.datasets_)
        n_datasets = self.datasets_.shape[0]

        X_list = []
        y_bin_list = []
        self.classes_list_ = []

        if do_validation:
            X_val_list = []
            y_bin_val_list = []

        for this_dataset in self.datasets_:
            indices = datasets == this_dataset
            this_X = X[indices]
            this_y = y[indices]
            label_binarizer = LabelBinarizer()
            classes = np.unique(this_y)
            self.classes_list_.append(classes)
            this_y_bin = label_binarizer.fit_transform(this_y)
            y_bin_list.append(this_y_bin)
            X_list.append(this_X)

            if do_validation:
                indices = datasets_val == this_dataset
                this_X_val = X_val[indices]
                this_y_val = y_val[indices]
                this_y_bin_val = label_binarizer.fit_transform(this_y_val)
                y_bin_val_list.append(this_y_bin_val)
                X_val_list.append(this_X_val)

        self.classes_ = np.concatenate(self.classes_list_)

        # Model construction
        from keras.callbacks import EarlyStopping, History
        import keras.backend as K
        init_tensorflow(n_jobs=self.n_jobs)

        if self.latent_dim is not None:
            self.models_, self.stacked_model_ = \
                make_factored_model(n_features,
                                    classes_list=self.classes_list_,
                                    beta=self.beta,
                                    alpha=self.alpha,
                                    latent_dim=self.latent_dim,
                                    activation=self.activation,
                                    dropout=self.dropout,
                                    optimizer=self.optimizer,
                                    fit_intercept=self.fit_intercept, )
        else:
            self.models_, self.stacked_model_ = \
                make_simple_model(n_features,
                                  classes_list=self.classes_list_,
                                  beta=self.beta,
                                  alpha=self.alpha,
                                  dropout=self.dropout,
                                  optimizer=self.optimizer,
                                  fit_intercept=self.fit_intercept, )
            self.fine_tune = False
        self.n_samples_ = [len(this_X_list) for this_X_list in X_list]

        if do_validation and self.early_stop:
            early_stoppings = []
            for model in self.models_:
                early_stopping = EarlyStopping(
                    monitor='val_loss',
                    min_delta=1e-3,
                    patience=3,
                    verbose=1,
                    mode='auto')
                early_stopping.set_model(model)
                early_stoppings.append(early_stopping)

        self.histories_ = []
        for model in self.models_:
            history = History()
            history.set_model(model)
            self.histories_.append(history)
        # Optimization loop
        n_epochs = np.zeros(n_datasets)
        batches_list = []
        for i, (this_X, this_y_bin) in enumerate(zip(X_list, y_bin_list)):
            permutation = self.random_state.permutation(len(this_X))
            this_X[:] = this_X[permutation]
            this_y_bin[:] = this_y_bin[permutation]
            batches = gen_batches(len(this_X), self.batch_size)
            batches_list.append(batches)
        epoch_logs = {}
        stop_training = False
        if do_validation:
            for history in self.histories_:
                history.on_train_begin()
            if self.early_stop:
                for early_stopping in early_stoppings:
                    early_stopping.on_train_begin()
        while not stop_training and np.min(n_epochs) < self.max_iter:
            if do_validation:
                for model in self.models_:
                    model.stop_training = stop_training
            for i, model in enumerate(self.models_):
                this_X = X_list[i]
                this_y_bin = y_bin_list[i]
                if do_validation:
                    this_X_val = X_val_list[i]
                    this_y_bin_val = y_bin_val_list[i]
                try:
                    batch = next(batches_list[i])
                except StopIteration:
                    this_X = X_list[i]
                    permutation = self.random_state.permutation(len(this_X))
                    this_X[:] = this_X[permutation]
                    this_y_bin[:] = this_y_bin[permutation]
                    batches_list[i] = gen_batches(len(this_X), self.batch_size)
                    n_epochs[i] += 1
                    loss, acc = model.evaluate(this_X, this_y_bin, verbose=0)
                    epoch_logs['loss'] = loss
                    epoch_logs['acc'] = acc
                    if do_validation:
                        val_loss, val_acc = model.evaluate(this_X_val,
                                                           this_y_bin_val,
                                                           verbose=0)
                        epoch_logs['val_loss'] = val_loss
                        epoch_logs['val_acc'] = val_acc
                        if self.early_stop:
                            early_stoppings[i].on_epoch_end(n_epochs[i],
                                                            epoch_logs)
                        self.histories_[i].on_epoch_end(n_epochs[i],
                                                        epoch_logs)
                    else:
                        val_acc = 0
                        val_loss = 0
                    if self.verbose:
                        print('Epoch %i, dataset %i, loss: %.4f, acc: %.4f, '
                              'val_acc: %.4f, val_loss: %.4f' %
                              (n_epochs[i], self.datasets_[i],
                               loss, acc, val_acc, val_loss))
                    batch = next(batches_list[i])
                model.train_on_batch(this_X[batch], this_y_bin[batch])
                if do_validation and self.early_stop:
                    stop_training = np.all(np.array([model.stop_training
                                                     for model in
                                                     self.models_]))
        if do_validation and self.early_stop:
            for early_stopping in early_stoppings:
                early_stopping.on_train_end()
        self.n_epochs_ = n_epochs
        if self.fine_tune:
            print('Fine tuning last layer')
            for i, model in enumerate(self.models_):
                lr = K.get_value(self.models_[i].optimizer.lr) * 0.1
                K.set_value(self.models_[i].optimizer.lr, lr)
                self.models_[i].layers_by_depth[3][0].trainable = False
                self.models_[i].layers_by_depth[2][0].rate = 0
                this_X = X_list[i]
                this_y_bin = y_bin_list[i]
                if do_validation:
                    this_X_val = X_val_list[i]
                    this_y_bin_val = y_bin_val_list[i]
                    if self.early_stop:
                        callbacks = [EarlyStopping(
                            monitor='val_loss',
                            min_delta=1e-3,
                            patience=3,
                            verbose=1,
                            mode='auto')]
                    else:
                        callbacks = None
                    model.fit(this_X, this_y_bin,
                              validation_data=(this_X_val, this_y_bin_val),
                              callbacks=callbacks,
                              epochs=self.max_iter,
                              verbose=self.verbose)
                else:
                    model.fit(this_X, this_y_bin, epochs=30)

    def predict_proba(self, X, dataset=None):
        if dataset is None:
            return self.stacked_model_.predict(X)
        else:
            idx = np.where(self.datasets_ == dataset)[0][0]
            return self.models_[idx].predict(X)

    def predict(self, X):
        X = check_array(X, accept_sparse='csr', order='C', dtype=np.float32)
        datasets = X[:, -1].astype('int')
        X = X[:, :-1]
        pred = np.zeros(X.shape[0], dtype='int')
        indices = np.logical_not(np.any(self.datasets_[:, np.newaxis]
                                        == datasets, axis=1))
        if np.sum(indices) > 0:
            this_X = X[indices]
            pred[indices] = self.classes_[np.argmax(self.predict_proba(this_X),
                                                    axis=1)]
        for this_dataset in self.datasets_:
            indices = datasets == this_dataset
            if np.sum(indices) > 0:
                this_X = X[indices]
                these_classes = self.classes_list_[this_dataset]
                pred[indices] = these_classes[np.argmax(self.predict_proba(
                    this_X, dataset=this_dataset), axis=1)]
        return pred

    def score(self, X, y, **kwargs):
        X, y = check_X_y(X, y, accept_sparse='csr', dtype=np.float32,
                         order="C")
        datasets = X[:, -1].astype('int')
        X = X[:, :-1]
        accs = []
        for i, this_dataset in enumerate(self.datasets_):
            indices = datasets == this_dataset
            this_X = X[indices]
            this_y = y[indices]
            label_binarizer = LabelBinarizer().fit(self.classes_list_[i])
            model = self.models_[i]
            this_y_bin = label_binarizer.fit_transform(this_y)
            loss, acc = model.evaluate(this_X, this_y_bin, verbose=0)
            accs.append(acc)
        return np.mean(np.array(accs))


def make_factored_model(n_features,
                        classes_list,
                        alpha=0.01, beta=0.01,
                        latent_dim=10, activation='linear', optimizer='adam',
                        fit_intercept=True,
                        dropout=0, ):
    from keras.engine import Input
    from keras.layers import Dense, Activation, Concatenate, Dropout
    from keras.regularizers import l2, l1_l2
    from .fixes import OurModel

    input = Input(shape=(n_features,), name='input')

    kernel_regularizer_enc = l1_l2(l1=beta, l2=alpha)
    kernel_regularizer_sup = l2(alpha)
    encoded = Dense(latent_dim, activation=activation,
                    use_bias=False, kernel_regularizer=kernel_regularizer_enc,
                    name='encoded')(input)
    if dropout > 0:
        encoded = Dropout(rate=dropout)(encoded)
    models = []
    supervised_list = []
    for classes in classes_list:
        n_classes = len(classes)
        supervised = Dense(n_classes, activation='linear',
                           use_bias=fit_intercept,
                           kernel_regularizer=kernel_regularizer_sup)(encoded)
        supervised_list.append(supervised)
        softmax = Activation('softmax')(supervised)
        model = OurModel(inputs=input, outputs=softmax)
        model.compile(optimizer=optimizer,
                      loss='categorical_crossentropy',
                      metrics=['accuracy'])
        models.append(model)
    if len(supervised_list) > 1:
        stacked = Concatenate(axis=1)(supervised_list)
    else:
        stacked = supervised_list[0]
    softmax = Activation('softmax')(stacked)
    stacked_model = OurModel(inputs=input, outputs=softmax)
    stacked_model.compile(optimizer=optimizer,
                          loss='categorical_crossentropy',
                          metrics=['accuracy'])
    return models, stacked_model


def make_simple_model(n_features, classes_list, alpha=0.01, beta=0.01,
                      optimizer='adam',
                      fit_intercept=True,
                      dropout=0, ):
    from keras.engine import Input
    from keras.layers import Dense, Activation, Concatenate, Dropout
    from keras.regularizers import l1_l2
    from .fixes import OurModel

    input = Input(shape=(n_features,), name='input')

    kernel_regularizer_sup = l1_l2(l1=beta, l2=alpha)
    if dropout > 0:
        encoded = Dropout(rate=dropout)(input)
    else:
        encoded = input
    models = []
    supervised_list = []
    for classes in classes_list:
        n_classes = len(classes)
        supervised = Dense(n_classes, activation='linear',
                           use_bias=fit_intercept,
                           kernel_regularizer=kernel_regularizer_sup)(encoded)
        supervised_list.append(supervised)
        softmax = Activation('softmax')(supervised)
        model = OurModel(inputs=input, outputs=softmax)
        model.compile(optimizer=optimizer,
                      loss='categorical_crossentropy',
                      metrics=['accuracy'])
        models.append(model)
    if len(supervised_list) > 1:
        stacked = Concatenate(axis=1)(supervised_list)
    else:
        stacked = supervised_list[0]
    softmax = Activation('softmax')(stacked)
    stacked_model = OurModel(inputs=input, outputs=softmax)
    stacked_model.compile(optimizer=optimizer,
                          loss='categorical_crossentropy',
                          metrics=['accuracy'])
    return models, stacked_model


def init_tensorflow(n_jobs=1):
    import tensorflow as tf
    from keras.backend import set_session
    sess = tf.Session(
        config=tf.ConfigProto(
            device_count={'CPU': n_jobs},
            inter_op_parallelism_threads=n_jobs,
            intra_op_parallelism_threads=n_jobs,
            use_per_session_threads=True)
    )
    set_session(sess)
