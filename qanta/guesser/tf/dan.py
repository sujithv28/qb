from collections import ChainMap, Counter, defaultdict

import heapq
from itertools import chain, repeat
import numpy as np
import os
from pathlib import Path
import pickle
from qanta import logging
from qanta.datasets.quiz_bowl import QuizBowlDataset
from qanta.guesser.abstract import AbstractGuesser
from qanta.guesser.dan_tf import _create_embeddings
from qanta.guesser.util.dataset import get_or_make_id_map, get_all_questions
from qanta.preprocess import preprocess_dataset, tokenize_question
from qanta.util.constants import (DEEP_DAN_PARAMS_TARGET, DEEP_EXPERIMENT_S3_BUCKET,
                                  DEEP_EXPERIMENT_FLAG, DEEP_TF_PARAMS_TARGET, N_GUESSES)
from qanta.util.environment import QB_TF_EXPERIMENT_ID
from qanta.util.io import shell, safe_open
import random
import sys
import tempfile
import tensorflow as tf
import time

QUIZ_BOWL_DS = 'qb'
WIKI_DS = 'wiki'

sys.setrecursionlimit(4096)

log = logging.get(__name__)


def _make_layer(i, in_tensor, n_out, op, n_in=None, dropout_prob=None):
    W = tf.get_variable('W' + str(i), (in_tensor.get_shape()[1] if n_in is None else n_in, n_out), dtype=tf.float32)
    if dropout_prob is not None:
        W = tf.nn.dropout(W, keep_prob=1 - dropout_prob)
    b = tf.get_variable('b' + str(i), n_out, dtype=tf.float32)
    out = tf.matmul(in_tensor, W) + b
    return (out if op is None else op(out)), W


class TFDan(AbstractGuesser):
    def __init__(self, **params):
        self._is_train = params.get('is_train', True)
        self._batch_size = params.get('batch_size', 128)
        self._max_epochs = params.get('max_epochs', 50)
        self._init_scale = params.get('init_scale', 0.06)
        self._word_drop = params.get('word_drop', 0.3)
        self._lstm_representation = params.get('lstm_representation', False)
        self._lstm_dropout_prob = params.get('lstm_dropout_prob', 0.5)
        self._prediction_dropout_prob = params.get('prediction_dropout_prob', 0.5)
        self._adversarial = params.get('adversarial', False)
        self._adversarial_interval = params.get('adversarial_interval', 3)
        self._use_weights = params.get('use_weights', False)
        self._hidden_units = params.get('hidden_units', 300)
        self._adversarial_units = params.get('adversarial_units', 301)
        self._n_prediction_layers = params.get('n_prediction_layers', 2)
        self._domain_classifier_weight = params.get('domain_classifier_weight', 0.02)
        self._n_representation_layers = params.get('n_representation_layers', 2)
        self._learning_rate = params.get('learning_rate', 0.0001)
        self._l2_rho = params.get('rho', 10**-5)
        self._dataset_weights = params.get('dataset_weights', None)
        self._label_map = params.get('label_map', None)
        self._embedding_shape = params.get('embedding_shape', None)
        self._word_ids = params.get('word_ids', None)
        self._params = params


        if self._dataset_weights is None:
            self._dataset_weights = {QUIZ_BOWL_DS: 1, WIKI_DS: 1}

    def __enter__(self):
        # self._graph_manager = tf.Graph().as_default()
        # self._graph_manager.__enter__()
        self._session = tf.Session()
        self._session.__enter__()

        self._var_scope = tf.variable_scope(
            'dan',
            reuse=(None if self._is_train else True),
            initializer=tf.random_uniform_initializer(minval=-self._init_scale, maxval=self._init_scale))
        self._var_scope.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # self._graph_manager.__exit__(exc_type, exc_value, traceback)
        self._var_scope.__exit__(exc_type, exc_value, traceback)
        self._session.__exit__(exc_type, exc_value, traceback)

    def _setup(self, data):
        if self._is_train:
            (self._data,
             self._labels,
             self._lens,
             self._weights,
             self._domains,
             self._val_data,
             self._val_labels,
             self._val_lens,
             embeddings,
             self._domain_indices,
             self._n_classes,
             self._complete,
             self._max_len,
             self._label_map,
             self._word_ids,
             self._answer_counts) = self._load_data(datasets=data)
        else:
            self._n_classes = len(self._label_map)
            embeddings = None

        log.info('Building model')
        self._build_model(initial_embed=embeddings)

        self._saver = tf.train.Saver()

    def train(self, data):
        with tf.Graph().as_default(), tf.Session() as self._session,\
            tf.variable_scope('dan', reuse=None, initializer=tf.random_uniform_initializer(minval=-self._init_scale, maxval=self._init_scale)):

            self._setup(data=data)
            return self._run_training()

    def evaluate(self, data, label_map, embedding_shape, n_classes, word_ids):
        self._setup(data=data, label_map=label_map, initial_embed=None, embedding_shape=embedding_shape, n_classes=n_classes, word_ids=word_ids)
        return self._evaluate()

    def _build_model(
            self,
            initial_embed):
        if initial_embed is not None:
            self._embedding = tf.get_variable('embedding', initializer=tf.constant(initial_embed, dtype=tf.float32))
            self._embedding_shape = np.shape(initial_embed)
        else:
            self._embedding = tf.get_variable('embedding', shape=self._embedding_shape, dtype=tf.float32)

        embed_and_zero = tf.pad(self._embedding, [[1, 0], [0, 0]], mode='CONSTANT')

        batch_dim = self._batch_size
        self._input_placeholder = tf.placeholder(tf.int32, shape=(batch_dim, None), name='input_placeholder')
        self._len_placeholder = tf.placeholder(tf.float32, shape=batch_dim, name='len_placeholder')
        self._label_placeholder = tf.placeholder(tf.int32, shape=batch_dim, name='label_placeholder')
        if self._use_weights:
            self._weight_placeholder = tf.placeholder(tf.float32, shape=None, name='weight_placeholder')
        if self._adversarial:
            self._domain_gate_placeholder = tf.placeholder(tf.float32, shape=(), name='domain_gate_placeholder')
            self._domain_classifier_weight_placeholder = tf.placeholder(tf.float32, shape=batch_dim, name='domain_weight_placeholder')
            self._domain_placeholder = tf.placeholder(tf.float32, shape=None, name='domain_placeholder')
            self._unlabeled_placeholder = tf.placeholder(tf.int32, shape=(batch_dim, None), name='unlabeled_placeholder')
            self._unlabeled_len_placeholder = tf.placeholder(tf.float32, shape=batch_dim, name='unlabeled_len_placeholder')

        # Store layer weights for use in regularization
        weights = []

        # (batch_size, embedding_dim) mean of embeddings
        with tf.variable_scope('representation'):
            self._representation_layer = self._make_representation(embedding=embed_and_zero,
                                                                   input_ids=self._input_placeholder,
                                                                   lengths=self._len_placeholder)

        if self._adversarial:
            with tf.variable_scope('representation', reuse=True):
                unlabeled_representation = self._make_representation(embedding=embed_and_zero,
                                                                     input_ids=self._unlabeled_placeholder,
                                                                     lengths=self._unlabeled_len_placeholder)

        with tf.variable_scope('prediction_net'):
            layer_out = self._representation_layer
            in_dim = self._hidden_units
            self._prediction_dropout_var = tf.get_variable('prediction_dropout', (), dtype=tf.float32, trainable=False)
            for i in range(self._n_prediction_layers - 1):
                layer_out, w = _make_layer(i, layer_out, n_in=in_dim, n_out=self._hidden_units, op=tf.nn.relu, dropout_prob=self._prediction_dropout_var)
                weights.append(w)
                in_dim = None

            self._logits, w = _make_layer(self._n_prediction_layers - 1, layer_out, n_out=self._n_classes, op=None)
            weights.append(w)
            self._softmax_weights = weights[-1]
            # logits = logits - tf.expand_dims(tf.reduce_max(logits, 1), 1)

            self._loss = tf.nn.sparse_softmax_cross_entropy_with_logits(self._logits, tf.to_int64(self._label_placeholder))
        if self._use_weights:
            self._loss *= self._weight_placeholder
            self._loss = tf.reduce_sum(self._loss) / tf.reduce_sum(self._weight_placeholder)
        else:
            self._loss = tf.reduce_mean(self._loss)

        if self._adversarial:
            domain_loss, layers = self._build_domain_classifier(
                self._representation_layer,
                unlabeled_representation)
            self._domain_loss = domain_loss
            weights.extend(layers)
            # Downeighting of domain loss happens in modified gradient
            self._loss += domain_loss

        if self._l2_rho > 0:
            for W in weights:
                self._loss += tf.nn.l2_loss(W) * self._l2_rho

        # Used for labeling
        self._softmax_output = tf.nn.softmax(self._logits)

        self._preds = tf.to_int32(tf.argmax(self._logits, 1))
        # correct_labels = tf.to_int32(tf.argmax(self._label_placeholder, 1))
        self._batch_accuracy = tf.contrib.metrics.accuracy(self._preds, self._label_placeholder)
        self._accuracy, self._accuracy_update = tf.contrib.metrics.streaming_accuracy(self._preds, self._label_placeholder)
        if not self._is_train:
            return

        # optimizer = tf.train.AdagradOptimizer(learning_rate=0.01)
        optimizer = tf.train.AdamOptimizer(learning_rate=self._learning_rate)
        self._train_op = optimizer.minimize(self._loss)

    def _make_representation(self, embedding, input_ids, lengths):
        # (batch_size, max_len, embedding_dim)
        sent_vecs = tf.nn.embedding_lookup(embedding, input_ids)

        # Apply dropout at word level
        if self._is_train and self._word_drop > 0:
            self._word_drop_var = tf.get_variable('word_drop', (), dtype=tf.float32, trainable=False)
            drop_filter = tf.nn.dropout(tf.ones((self._max_len, 1)), keep_prob=(1 - self._word_drop_var))
            sent_vecs = sent_vecs * drop_filter

        if self._lstm_representation:
            return self._build_lstm(
                input_layer=sent_vecs,
                lengths=lengths)
        else:
            layer_out = tf.reduce_sum(sent_vecs, 1) / tf.expand_dims(lengths, 1)
            in_dim = embedding.get_shape()[1]
            for i in range(self._n_representation_layers):
                layer_out, w = _make_layer(i, layer_out, n_in=in_dim, n_out=self._hidden_units, op=tf.nn.relu)
                in_dim = None

            return layer_out

    def _build_domain_classifier(self, primary_input, opposite_domain_input, n_layers=2):
        grad_name = 'GradientReversal' + str(id(self))

        @tf.RegisterGradient(grad_name)
        def gradient_reversal(op, grads):
            return -grads * self._domain_classifier_weight_placeholder * self._domain_gate_placeholder

        first_loop = True
        weights = []
        losses = []
        accuracies = []
        # Runs twice, once for domain of example being classified, and once for an unlabeled example
        # from the other domain
        for input_layer in (primary_input, opposite_domain_input):
            with tf.variable_scope('domain_classifier', reuse=(None if first_loop else True)):
                with tf.get_default_graph().gradient_override_map({'Identity': grad_name}):
                    # batch_size, num_units
                    reversal_layer = tf.identity(input_layer)
                layer_out = reversal_layer
                for i in range(n_layers - 1):
                    layer_out, w = _make_layer(i, layer_out, n_out=self._adversarial_units, op=tf.nn.relu)
                    weights.append(w)

                logits, w = _make_layer(n_layers - 1, layer_out, n_out=1, op=None)
                weights.append(w)
                domain_preds = tf.squeeze(tf.to_int32(tf.round(tf.nn.sigmoid(logits))), (1,))
                true_domains = self._domain_placeholder if first_loop else 1 - self._domain_placeholder
                accuracies.append(tf.contrib.metrics.accuracy(domain_preds, tf.to_int32(true_domains)))

                losses.append(tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits, true_domains)))
            first_loop = False
            self._batch_domain_accuracy = tf.reduce_mean(accuracies)
        return tf.reduce_sum(losses), weights

    def _build_lstm(self, input_layer, lengths):
        self._lstm_max_len = tf.get_variable('lstm_max_len', dtype=tf.int32, initializer=tf.constant(-1), trainable=False)
        cell = tf.nn.rnn_cell.LSTMCell(self._hidden_units, forget_bias=1.0, state_is_tuple=True, use_peepholes=True)
        if self._is_train:
            self._lstm_dropout_var = tf.get_variable('lstm_dropout', (), dtype=tf.float32, trainable=False)
            cell = tf.nn.rnn_cell.DropoutWrapper(cell,
                                                 output_keep_prob=1 - self._lstm_dropout_var,
                                                 input_keep_prob=1 - self._lstm_dropout_var)

        cell = tf.nn.rnn_cell.MultiRNNCell([cell] * self._n_representation_layers, state_is_tuple=True)
        initial_state = cell.zero_state(self._batch_size if self._is_train else 1, tf.float32)
        outputs, state = tf.nn.dynamic_rnn(cell, input_layer, sequence_length=lengths, initial_state=initial_state, parallel_iterations=128)
        # Select just the last output from each example
        outputs = tf.reshape(outputs, (-1, self._hidden_units))
        indices = (tf.range(self._batch_size) * self._lstm_max_len + tf.to_int32(lengths) - 1)
        outputs = tf.gather(outputs, indices)
        return outputs

    def _load_data(self, datasets, len_limit=200):
        """Load training data"""
        preprocessed_datasets = {}
        vocab = set()
        class_to_i = {}
        i_to_class = []
        x_val = []
        y_val = []
        for dataset_name, dataset in sorted(datasets.items(), key=lambda n: n != QUIZ_BOWL_DS):
            results = preprocess_dataset(
                dataset,
                train_size=0.9 if dataset_name == QUIZ_BOWL_DS else 1,
                vocab=vocab,
                class_to_i=class_to_i,
                i_to_class=i_to_class)
            preprocessed_datasets[dataset_name] = results[:2]
            x_val.extend(results[2])
            y_val.extend(results[3])

        embeddings, word_ids = _create_embeddings(vocab)
        embeddings = np.vstack((embeddings, np.zeros(embeddings[0].shape), np.zeros(embeddings[0].shape)))
        start_token_index = embeddings.shape[0] - 2
        end_token_index = embeddings.shape[0] - 1
        unk_index = word_ids['UNK']

        vecs = []
        labels = []

        complete = []
        weights = []
        domains = []
        example_counts = Counter()
        max_len = 0
        for dataset_name, (questions, answers) in preprocessed_datasets.items():
            log.info('Loading', dataset_name)
            weight = self._dataset_weights[dataset_name]
            if weight == 0:
                continue
            q_count = 0
            for run, label in zip(questions, answers):
                q_count += 1
                q = [start_token_index]
                example_counts[label] += 1

                q.extend(word_ids.get(w, unk_index) for w in run)
                q.append(end_token_index)

                max_len = max(len(q), max_len)
                if len(q) > 2:
                    # Shift indices by 1 so that 0 can represent zero embedding
                    vecs.append([d + 1 for d in q])
                    labels.append(label)
                    weights.append(weight)
                    domains.append(dataset_name == WIKI_DS)

        val_vecs = []
        val_labels = []
        for run, label in zip(x_val, y_val):
            q = [start_token_index] + list(word_ids.get(w, len(word_ids)) for w in run) + [end_token_index]
            q = q[:max_len]
            if len(q) > 2:
                val_vecs.append([d + 1 for d in q])
                val_labels.append(label)

        lens = []
        val_lens = []
        for vec_list, len_list in ((vecs, lens), (val_vecs, val_lens)):
            for v in vec_list:
                len_list.append(len(v))
                # After end of question, pad with zero embedding
                v.extend(repeat(0, max_len - len(v)))

        log.info('{} total examples'.format(len(vecs)))
        log.info('Max example len: {}'.format(max_len))

        # Only need to get number of classes if building a model from scratch
        labels = np.array(labels)
        n_classes = len(i_to_class)

        # Conversion of each v to array matters if data is jagged (which it will be for non-training models)
        data = np.array([np.array(v) for v in vecs])
        lens = np.array(lens)
        weights = np.array(weights)
        domains = np.array(domains)

        val_data = np.array([np.array(v) for v in val_vecs])
        val_labels = np.array(val_labels)
        val_lens = np.array(val_lens)

        domain_indices = [[], []]
        for i, d in enumerate(domains):
            domain_indices[d].append(i)

        log.info('Done loading data')
        return (
            data, labels, lens, weights, domains,
            val_data, val_labels, val_lens,
            embeddings,
            domain_indices, n_classes, complete, max_len, class_to_i, word_ids, example_counts)

    def _batches(self, train=True):
        data_arr = self._data if train else self._val_data
        len_arr = self._lens if train else self._val_lens
        label_arr = self._labels if train else self._val_labels
        order = [i for i in list(range(len(data_arr)))]
        np.random.shuffle(order)
        for indices in (order[i:(i + self._batch_size)] for i in range(0, len(order), self._batch_size)):
            if len(indices) == self._batch_size:
                yield ((data_arr[indices, :], len_arr[indices], label_arr[indices]) +
                    ((self._weights[indices], self._domains[indices]) if train else ()))

    def _guess_batches(self, data, lens):
        real_len = len(data)
        padded_len = real_len + (-real_len % self._batch_size)
        for i in range(0, padded_len, self._batch_size):
            batch_data = data[i : i + self._batch_size, :]
            batch_lens = lens[i : i + self._batch_size]
            if i + self._batch_size > real_len:
                batch_data = np.vstack(
                    (batch_data,
                    np.zeros((i + self._batch_size - real_len, len(data[0])))))
                batch_lens = np.concatenate((batch_lens, np.zeros(i + self._batch_size - real_len)))
            yield batch_data, batch_lens

    def _representation_batches(self, n):
        source_count = 0
        target_count = 0
        order = []
        holdout_set = set(chain(self._val_indices, self._wiki_holdout_indices))
        for i, (complete, domain) in enumerate(zip(self._complete, self._domains)):
            if not complete or i not in holdout_set:
                continue
            if domain and target_count < n:
                order.append(i)
                target_count += 1
            elif not domain and source_count < n:
                order.append(i)
                source_count += 1
            elif source_count == n and target_count == n:
                break

        for indices in (order[i:(i + self._batch_size)] for i in range(0, len(order), self._batch_size)):
            if len(indices) == self._batch_size:
                yield self._data[indices, :], self._lens[indices], self._domains[indices]

    def _run_epoch(self, session, epoch_num, train=True):
        total_loss = 0
        # Reset accuracy accumulators
        start_time = time.time()
        accuracies = []
        losses = []
        if self._lstm_representation and self._lstm_max_len:
            session.run(self._lstm_dropout_var.assign(self._lstm_dropout_prob if train else 0))
        # TODO: Refactor to use a self._word_drop variable rather than depending on self._lstm_representation
        else:
            session.run(self._word_drop_var.assign(self._word_drop if train else 0))

        session.run(self._prediction_dropout_var.assign(self._prediction_dropout_prob if train else 0))

        if self._adversarial:
            domain_accuracies = []
        for i, batch in enumerate(self._batches(train=train)):
            if train:
                inputs, lens, labels, weights, domains = batch
            else:
                inputs, lens, labels = batch

            batch_start = time.time()
            fetches = ((self._loss, self._batch_accuracy, self._train_op)
                       if train else
                       (self._loss, self._batch_accuracy))
            feed_dict = {self._input_placeholder: inputs,
                         self._len_placeholder: lens,
                         self._label_placeholder: labels}
            if self._use_weights and train:
                feed_dict[self._weight_placeholder] = weights
            if self._adversarial and train:
                feed_dict[self._domain_gate_placeholder] = 1  # int(not i % self._adversarial_interval)
                feed_dict[self._domain_placeholder] = domains
                # Create sample of opposite domains
                indices = [self._domain_indices[not d][random.randint(0, len(self._domain_indices[not d]) - 1)]
                           for d in domains]

                feed_dict[self._unlabeled_placeholder] = self._data[indices, :]
                feed_dict[self._unlabeled_len_placeholder] = self._lens[indices]
                fetches += (self._domain_loss,)
                fetches += (self._batch_domain_accuracy,)

            # run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            # run_metadata = tf.RunMetadata()
            # loss, *_ = session.run(fetches, feed_dict=feed_dict, options=run_options, run_metadata=run_metadata)
            loss, accuracy, *others = session.run(fetches, feed_dict=feed_dict)
            accuracies.append(accuracy)
            if self._adversarial:
                domain_accuracy = others[-1]
                domain_accuracies.append(domain_accuracy)

            # summary_writer.add_run_metadata(run_metadata, 'step{}'.format(i))
            total_loss += loss
            losses.append(loss)
            batch_duration = time.time() - batch_start
            if self._adversarial:
                log.info('{} Epoch: {} Batch: {} Accuracy: {:.4f} Loss: {:.4f} Domain Accuracy: {:.4f}, Duration: {:.4f}'.format(
                    'Train' if train else 'Val', epoch_num, i, accuracy, loss, domain_accuracy, batch_duration))
            else:
                log.info('{} Epoch: {} Batch: {} Accuracy: {:.4f} Loss: {:.4f} Duration: {:.4f}'.format(
                    'Train' if train else 'Val', epoch_num, i, accuracy, loss, batch_duration))

        duration = time.time() - start_time

        return (accuracies, losses, duration) + ((domain_accuracies,) if self._adversarial else ())

    def _recall_at_n(self, probs, n_max=N_GUESSES):
        """Compute recall@N for all N up to n_max"""
        # Get indices of all examples which are full questions
        num_correct = 0
        ordered_probs = [(i, heapq.nlargest(n_max, range(self._n_classes), key=p.__getitem__)) for i, p in probs]
        total = len(ordered_probs)
        incorrect = {i: j for i, (j, _) in enumerate(ordered_probs)}
        result = []
        for i in range(0, n_max):
            log.info('Computing recall@{}'.format(i))
            to_remove = []
            for ex_num, label_index in incorrect.items():
                if ordered_probs[ex_num][1][i] == self._labels[label_index]:
                    to_remove.append(ex_num)
                    num_correct += 1
            for r in to_remove:
                del incorrect[r]

            result.append(num_correct / total)
        return result

    def _run_training(self):
        if not self._is_train:
            raise ValueError('To use a non-train model, call label() instead')
        self._session.run(tf.initialize_all_variables())
        if self._lstm_representation:
            self._session.run(self._lstm_max_len.assign(self._max_len))
        max_accuracy = -1

        train_accuracies = []
        train_losses = []

        holdout_accuracies = []
        holdout_losses = []

        if self._adversarial:
            train_domain_accuracies = []
            holdout_domain_accuracies = []

        max_patience = 15
        patience = max_patience
        for i in range(self._max_epochs):
            accuracies, losses, duration, *others = self._run_epoch(self._session, i)
            log.info('Train Epoch: {} Avg loss: {} Accuracy: {}. Ran in {} seconds.'.format(
                i, np.average(losses), np.average(accuracies), duration))
            train_accuracies.append(accuracies)
            train_losses.append(losses)

            if self._adversarial:
                domain_accuracies = others[0]
                log.info('Domain Accuracy: {}'.format(np.average(domain_accuracies)))
                train_domain_accuracies.append(domain_accuracies)

            val_accuracies, val_losses, val_duration, *others = self._run_epoch(self._session, i, train=False)
            val_accuracy = np.average(val_accuracies)
            log.info('Val Epoch: {} Avg loss: {} Accuracy: {}. Ran in {} seconds.'.format(
                i, np.average(val_losses), val_accuracy, val_duration))

            holdout_accuracies.append(val_accuracies)
            holdout_losses.append(val_losses)
            if self._adversarial:
                holdout_domain_accuracies.append(others[0])

            patience -= 1
            if val_accuracy > max_accuracy:
                max_accuracy = val_accuracy
                log.info('New best accuracy. Saving model')
                patience = max_patience
                with safe_open(DEEP_TF_PARAMS_TARGET, 'wb') as f:
                    pickle.dump(dict(ChainMap({'label_map': self._label_map, 'embedding_shape': self._embedding_shape, 'word_ids': self._word_ids}, self._params)), f)
                self._saver.save(self._session, DEEP_DAN_PARAMS_TARGET)

            if patience == 0:
                break

        return (train_losses,
                train_accuracies,
                train_domain_accuracies if self._adversarial else None,
                holdout_losses,
                holdout_accuracies,
                holdout_domain_accuracies if self._adversarial else None)

    def _evaluate(self):
        """Generate softmax output for all examples in dataset"""
        self.restore(DEEP_DAN_PARAMS_TARGET)
        self._session.run(tf.initialize_local_variables())
        results = []
        count = 0
        reverse_label_map = {i: l for l, i in self._label_map.items()}
        with open('/tmp/guess_file', 'w') as f:
            for i, (in_array, length, label, complete, domain) in enumerate(zip(self._data, self._lens, self._labels, self._complete, self._domains)):
                if not complete:
                    continue
                fetches = (self._softmax_output, self._accuracy_update, self._preds)
                feed_dict = {self._input_placeholder: in_array, self._len_placeholder: length, self._label_placeholder: label}
                feed_dict = {k: np.expand_dims(v, 0) for k, v in feed_dict.items()}
                if self._lstm_representation:
                    self._session.run(self._lstm_max_len.assign(length))
                softmax_output, _, preds = self._session.run(fetches, feed_dict=feed_dict)
                assert not np.isnan(softmax_output).any()
                f.write('{},{}\n'.format(reverse_label_map.get(label, 'Unknown'), reverse_label_map[preds[0]]))

                results.append((i, np.squeeze(softmax_output)))
                count += 1
                if count % 1000 == 0:
                    log.info('Labeled {} examples'.format(count))
            accuracy = self._session.run(self._accuracy)
            recalls = self._recall_at_n(results)
        return accuracy, recalls

    def get_representations(self, n=5000):
        self._saver.restore(self._session, DEEP_DAN_PARAMS_TARGET)
        saved_representations = [[], []]
        log.info('Computing representations')
        for i, (in_arrays, lengths, domains) in enumerate(self._representation_batches(n), 1):
            # Skip most examples to save time
            fetches = (self._representation_layer,)
            feed_dict = {self._input_placeholder: in_arrays,
                         self._len_placeholder: lengths}
            representations, = self._session.run(fetches, feed_dict=feed_dict)
            for rep, domain in zip(representations, domains):
                saved_representations[domain].append(rep.tolist())
            log.info('Computed representations for {} batches'.format(i))

        return representations

    def restore(self, path):
        self._saver.restore(self._session, str(path))

    def save(self, directory):
        params_path = Path(DEEP_DAN_PARAMS_TARGET)
        in_dir = params_path.parent
        out_dir = Path(directory)
        tf_file = params_path.name
        extra_params = Path(DEEP_TF_PARAMS_TARGET).name
        for f_name in (tf_file, tf_file + '.meta', 'checkpoint', extra_params):
            shell('cp {} {}'.format(in_dir / f_name, out_dir / f_name))

    def format_test_data(self, questions):
        data = []
        lens = []
        max_len = 0
        end_token_index = self._embedding_shape[0]
        start_token_index = self._embedding_shape[0] - 1
        unk_index = self._word_ids['UNK']
        for q in questions:
            row = [start_token_index]
            row.extend(self._word_ids.get(w, unk_index) for w in tokenize_question(q))
            row.append(end_token_index)
            lens.append(len(row))
            max_len = max(len(row), max_len)
            data.append(row)

        for row in data:
            row.extend(0 for _ in range(max_len - len(row)))
        return np.array(data), np.array(lens)


    def guess(self, questions, max_n_guesses):
        with tf.Graph().as_default(), tf.Session() as self._session,\
            tf.variable_scope('dan', reuse=None, initializer=tf.random_uniform_initializer(minval=-self._init_scale, maxval=self._init_scale)):
            self._setup(None)
            self.restore(DEEP_DAN_PARAMS_TARGET)

            i_to_class = {i: c for c, i in self._label_map.items()}
            data, lens = self.format_test_data(questions)

            guesses = []
            for i, (x_batch, len_batch) in enumerate(self._guess_batches(data, lens)):
                feed_dict = {self._input_placeholder: x_batch, self._len_placeholder: len_batch}
                batch_logits = self._session.run(self._logits, feed_dict=feed_dict)
                guesses.extend([(i_to_class[i], row[i]) for i in np.argsort(row)[:-max_n_guesses - 1:-1]] for row in batch_logits)
        return guesses[:len(questions)]

    @classmethod
    def load(cls, directory):
        dir_path = Path(directory)
        with (dir_path / Path(DEEP_TF_PARAMS_TARGET).name).open('rb') as f:
            params = pickle.load(f)
        shell('cp -r {}/* {}'.format(dir_path, Path(DEEP_DAN_PARAMS_TARGET).parent))
        dan = cls(**ChainMap({'is_train': False}, params))
        return dan

    @classmethod
    def display_name(cls):
        return 'DomainDAN'

    @property
    def requested_datasets(self):
        return {QUIZ_BOWL_DS: QuizBowlDataset(5)}

    @classmethod
    def targets(cls):
        return [Path(DEEP_DAN_PARAMS_TARGET).name, Path(DEEP_TF_PARAMS_TARGET).name]

    @property
    def n_classes(self):
        return self._n_classes

    @property
    def embedding_shape(self):
        return self._embedding.get_shape()

    @property
    def label_map(self):
        return self._label_map

    @property
    def word_ids(self):
        return self._word_ids



def run_experiment(params, outfile):
    use_qb = params['use_qb']
    use_wiki = params['use_wiki']
    exclude_keys = {'use_qb', 'use_wiki', 'wiki_data_frac'}
    model_params = {k: v for k, v in params.items() if k not in exclude_keys}
    dataset_weights = {QUIZ_BOWL_DS: 1, WIKI_DS: int(use_wiki)}

    with tf.Graph().as_default():
        with TFDan(is_train=True, dataset_weights=dataset_weights, **model_params) as train_model:
            domains = []
            if use_qb:
                domains.append(QUIZ_BOWL_DS)
            if use_wiki:
                domains.append(WIKI_DS)
            train_data = get_all_questions(domains)
            (train_losses,
             train_accuracies,
             train_domain_accuracies,
             holdout_losses,
             holdout_accuracies,
             holdout_domain_accuracies) = train_model.train(train_data)
            train_model.restore(DEEP_DAN_PARAMS_TARGET)
            representations = train_model.get_representations() if params['adversarial'] else None
            train_weight = train_model._session.run(train_model._softmax_weights)

        del train_data
        with TFDan(is_train=False, dataset_weights=dataset_weights, initial_embed=None, **model_params) as dev_model:

            print('Loading val data')
            val_data = get_all_questions([QUIZ_BOWL_DS], folds=['dev'])
            val_list = list(val_data[QUIZ_BOWL_DS])
            val_data[QUIZ_BOWL_DS] = val_list
            print('Got {} val examples'.format(len(val_list)))
            print('Evaluating')
            dev_accuracy, dev_recalls = dev_model.evaluate(
                val_data,
                n_classes=train_model.n_classes,
                embedding_shape=train_model.embedding_shape,
                label_map=train_model.label_map,
                word_ids=train_model.word_ids)

            dev_weight = dev_model._session.run(dev_model._softmax_weights)

            print('Weights same: {}'.format((train_weight == dev_weight).all()))
            log.info('Accuracy on dev: {}'.format(dev_accuracy))
        result = {'params': params,
                  'train_losses': train_losses,
                  'train_accuracies': train_accuracies,
                  'train_domain_accuracies': train_domain_accuracies,
                  'holdout_losses': holdout_losses,
                  'holdout_accuracies': holdout_accuracies,
                  'holdout_domain_accuracies': holdout_accuracies,
                  'recalls': dev_recalls,
                  'representations': representations
                  }

    _write_result_to_s3(result)

    # Touch flag file to let luigi know experiment is done
    open(DEEP_EXPERIMENT_FLAG, 'w').close()


def _write_result_to_s3(result):
    _, f_name = tempfile.mkstemp()
    with open(f_name, 'wb') as f:
        pickle.dump(result, f)
    shell('aws s3 cp {} {}/{}'.format(f_name, DEEP_EXPERIMENT_S3_BUCKET, QB_TF_EXPERIMENT_ID))
    os.remove(f_name)


if __name__ == '__main__':
    run_experiment({'init_scale': 0.08,
                    'use_qb': True,
                    'use_wiki': True,
                    'adversarial': True,
                    'max_epochs': 70,
                    'n_prediction_layers': 2,
                    'lstm_representation': False,
                    'n_representation_layers': 2,
                    'adversarial_units': 300,
                    'domain_classifier_weight': 0.3,
                    'learning_rate': 0.0001,
                    'hidden_units': 300,
                    'lstm_dropout_prob': 0.5,
                    'prediction_dropout_prob': 0.5,
                    'word_drop': 0.3,
                    'rho': 10**-5,
                    'batch_size': 128}, outfile='/tmp/exp_output')