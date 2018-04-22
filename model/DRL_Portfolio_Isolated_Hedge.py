# -*- coding:utf-8 -*-
import tensorflow as tf
import numpy as np
import os
import tflearn as tl

# This model was inspired by
# Deep Direct Reinforcement Learning for Financial Signal Representation and Trading

'''
Model interpretation:
    inputs:
    f:  shape=(batch_size, feature_number), take any information you need and make a matrix in n rows and m columns
        n is the timestep for a batch, m is the number of features. Recommend to use technical indicators (MACD,RSI...)
        of assets you want to manage.
    z:  return of rate matrix, with n time-steps and k+1 assets (k assets and your cash pool)
    c:  transaction cost

    formulas:
    d_t = softmax(g(f,d_t-1...d_t-n)) where g is the complex non-linear transformation procedure, here we use GRU-rnn
        Here, d_t is the action, represent the predict portfolio weight generated by current information
        and previous several actions
    r_t = d_t-1*z_t-c*|d_t-d_t-1|
        r_t is the return of current time step, which is calculated by using previous predict action d_t-1 multiplies
        the return of rate of assets price in current step. Then, subtract transaction cost if the weight of holding assets
        changes.
    R = \sum_t(log(product(r_t)))
        The total log return
    object: max(R|theta)
        The objective is to maximize the total return.
'''


# feature_network_topology = {
#     'equity_network': {
#         'feature_map_number': 10,
#         'feature_number': 10,
#         'input_name': 'equity',
#         'dense': {
#             'n_units': [16, 32, 8],
#             'act': [tf.nn.tanh] * 3,
#         },
#         'rnn': {
#             'n_units': [8, 1],
#             'act': [tf.nn.tanh, None],
#             'attention_length': 5
#         },
#         'keep_output': True,
#     },
#     'index_network': {
#         'feature_map_number': 10,
#         'feature_number': 10,
#         'input_name': 'equity',
#         'dense': {
#             'n_units': [16, 32, 8],
#             'act': [tf.nn.tanh] * 3
#         },
#         'rnn': {
#             'n_units': [8, 2],
#             'act': [tf.nn.tanh, tf.nn.tanh],
#             'attention_length': 5
#         },
#         'keep_output': False,
#     }
# }


class DRL_Portfolio(object):
    def __init__(self, asset_number, feature_network_topology, object_function='sortino', learning_rate=0.001):
        tf.reset_default_graph()
        self.real_asset_number = asset_number + 1
        self.z = tf.placeholder(dtype=tf.float32, shape=[None, self.real_asset_number], name='environment_return')
        self.c = tf.placeholder(dtype=tf.float32, shape=[], name='environment_fee')
        self.dropout_keep_prob = tf.placeholder(dtype=tf.float32, shape=[], name='dropout_keep_prob')
        self.tao = tf.placeholder(dtype=tf.float32, shape=[], name='action_temperature')
        self.model_inputs = {}
        self.feature_outputs = []
        self.keep_output = None
        for k, v in feature_network_topology.items():
            with tf.variable_scope(k, initializer=tf.contrib.layers.xavier_initializer(), regularizer=tf.contrib.layers.l2_regularizer(0.1)):
                X = tf.placeholder(dtype=tf.float32, shape=[v['feature_map_number'], None, v['feature_number']], name=v['input_name'])
                self.model_inputs[k] = X
                # output = tl.layers.normalization.batch_normalization(X)
                output=X
                if 'dense' in v:
                    with tf.variable_scope(k + '/dense', initializer=tf.contrib.layers.xavier_initializer(), regularizer=tf.contrib.layers.l2_regularizer(0.1)):
                        dense_config = v['dense']
                        for n, a in zip(dense_config['n_units'], dense_config['act']):
                            output = self._add_dense_layer(output, output_shape=n, drop_keep_prob=self.dropout_keep_prob, act=a)
                            # output = tl.layers.normalization.batch_normalization(output)
                        tf.summary.histogram(k+'/dense_output',output)
                if 'rnn' in v:
                    with tf.variable_scope(k + '/rnn', initializer=tf.contrib.layers.xavier_initializer(), regularizer=tf.contrib.layers.l2_regularizer(0.1)):
                        rnn_config = v['rnn']
                        rnn_cells = [self._add_letm_cell(i, a) for i, a in list(zip(rnn_config['n_units'], rnn_config['act']))]
                        layered_cell = tf.contrib.rnn.MultiRNNCell(rnn_cells)
                        # if 'attention_length' in rnn_config.keys():
                        #     layered_cell = tf.contrib.rnn.AttentionCellWrapper(cell=layered_cell, attn_length=rnn_config['attention_length'])
                        layered_cell = tf.contrib.rnn.DropoutWrapper(layered_cell,
                                                                     input_keep_prob=self.dropout_keep_prob,
                                                                     output_keep_prob=self.dropout_keep_prob,
                                                                     state_keep_prob=self.dropout_keep_prob,
                                                                     )
                        output, state = tf.nn.dynamic_rnn(cell=layered_cell, inputs=output, dtype=tf.float32)
                        tf.summary.histogram(k + '/first_rnn_output', output)
                        if not v['keep_output']:
                            with tf.variable_scope(k + '/feature_map', initializer=tf.contrib.layers.xavier_initializer(), regularizer=tf.contrib.layers.l2_regularizer(0.1)):
                                feature_rnn_cell = self._add_letm_cell(self.real_asset_number, activation=tf.nn.tanh)
                                # if 'attention_length' in rnn_config.keys():
                                #     feature_rnn_cell = tf.contrib.rnn.AttentionCellWrapper(cell=feature_rnn_cell, attn_length=rnn_config['attention_length'])
                                feature_rnn_cell = tf.contrib.rnn.DropoutWrapper(feature_rnn_cell,
                                                                                 input_keep_prob=self.dropout_keep_prob,
                                                                                 output_keep_prob=self.dropout_keep_prob,
                                                                                 state_keep_prob=self.dropout_keep_prob,
                                                                                 )
                                feature_output, feature_state = tf.nn.dynamic_rnn(cell=feature_rnn_cell, inputs=output, dtype=tf.float32)
                                tf.summary.histogram(k + '/feature_rnn_output', feature_output)
                                feature_output = tf.unstack(feature_output, axis=0)
                            with tf.variable_scope(k + '/cash'):
                                cash_rnn_cell = self._add_letm_cell(1, activation=tf.nn.sigmoid)
                                # if 'attention_length' in rnn_config.keys():
                                #     cash_rnn_cell = tf.contrib.rnn.AttentionCellWrapper(cell=cash_rnn_cell, attn_length=rnn_config['attention_length'])
                                cash_rnn_cell = tf.contrib.rnn.DropoutWrapper(cash_rnn_cell,
                                                                              input_keep_prob=self.dropout_keep_prob,
                                                                              output_keep_prob=self.dropout_keep_prob,
                                                                              state_keep_prob=self.dropout_keep_prob,
                                                                              )
                                cash_output, cash_state = tf.nn.dynamic_rnn(cell=cash_rnn_cell, inputs=output, dtype=tf.float32)
                                tf.summary.histogram(k + '/cash_rnn_output', cash_output)
                                cash_output = tf.unstack(cash_output, axis=0)
                            if v['feature_map_number'] > 1:
                                feature_output = tl.layers.merge(feature_output, mode='elemwise_sum')/v['feature_map_number']
                                cash_output = tl.layers.merge(cash_output, mode='elemwise_sum')/ v['feature_map_number']
                            else:
                                feature_output = feature_output[0]
                                cash_output = cash_output[0]
                            self.feature_outputs.append((feature_output, cash_output))
                        else:
                            output = tf.unstack(output, axis=0)
                            if len(output)>1:
                                output = tl.layers.merge(output, mode='concat')
                            else:
                                output=output[0]
                            # output = tl.layers.normalization.batch_normalization(output)
                            self.keep_output = output
        with tf.name_scope('merge'):
            if len(self.feature_outputs) > 1:
                feature_maps = list(map(lambda x: x[0], self.feature_outputs))
                cash_maps = list(map(lambda x: x[1], self.feature_outputs))
                feature_maps = tl.layers.merge(feature_maps, mode='elemwise_sum')/len(self.feature_outputs)
                cash_maps = tl.layers.merge(cash_maps, mode='elemwise_sum') / len(self.feature_outputs)
            else:
                feature_maps = self.feature_outputs[0][0]
                cash_maps = self.feature_outputs[0][1]
            tf.summary.histogram('cash_map', cash_maps)
            tf.summary.histogram('feature_map', feature_maps)
            self.keep_output = tl.layers.merge([self.keep_output, cash_maps], mode='concat')
            self.keep_output = tl.layers.merge([self.keep_output, feature_maps], mode='elemwise_sum')
            tf.summary.histogram('keep_output', self.keep_output)
        with tf.variable_scope('action'):
            self.action = self.keep_output
            self.action = self.action / self.tao
            self.action = tf.nn.softmax(self.action)
            self.action = tf.concat([tf.nn.softmax(tf.random_uniform(shape=[1, self.real_asset_number])), self.action], axis=0)
        with tf.variable_scope('reward'):
            self.reward_t = tf.reduce_sum(self.z * self.action[:-1] - self.c * tf.abs(self.action[1:] - self.action[:-1]), axis=1)
            self.log_reward_t = tf.log(self.reward_t)
            self.cum_reward = tf.reduce_prod(self.reward_t)
            self.cum_log_reward = tf.reduce_sum(self.log_reward_t)
            self.mean_log_reward = tf.reduce_mean(self.log_reward_t)
            self.sortino = self._sortino_ratio(self.log_reward_t, 0)
            self.sharpe = self._sharpe_ratio(self.log_reward_t, 0)
            tf.summary.histogram('action', self.action)
            tf.summary.histogram('reward_t', self.reward_t)
            tf.summary.histogram('mean_log_reward', self.mean_log_reward)
        with tf.variable_scope('train'):
            optimizer = tf.train.RMSPropOptimizer(learning_rate=learning_rate)
            if object_function == 'reward':
                self.train_op = optimizer.minimize(-self.mean_log_reward)
            elif object_function == 'sharpe':
                self.train_op = optimizer.minimize(-self.sharpe)
            else:
                self.train_op = optimizer.minimize(-self.sortino)
        
        for var in tf.trainable_variables():
            tf.summary.histogram(var.op.name, var)
        self.session = tf.Session()
        self.saver = tf.train.Saver()
        self.init_op = tf.global_variables_initializer()
        self.merge_op = tf.summary.merge_all()
    
    def init_model(self):
        self.session.run(self.init_op)
    
    def get_session(self):
        return self.session
    
    def get_parameters(self):
        return tf.trainable_variables()
    
    def _add_dense_layer(self, inputs, output_shape, drop_keep_prob, act=tf.nn.tanh):
        output = tf.contrib.layers.fully_connected(activation_fn=act, num_outputs=output_shape, inputs=inputs)
        output = tf.nn.dropout(output, drop_keep_prob)
        return output
    
    def _sortino_ratio(self, r, rf):
        mean, var = tf.nn.moments(r, axes=[0])
        sign = tf.sign(-tf.sign(r - rf) + 1)
        number = tf.reduce_sum(sign)
        lower = sign * r
        square_sum = tf.reduce_sum(tf.pow(lower, 2))
        sortino_var = tf.sqrt(square_sum / number)
        sortino = (mean - rf) / sortino_var
        return sortino
    
    def _sharpe_ratio(self, r, rf):
        mean, var = tf.nn.moments(r - rf, axes=[0])
        return mean / var
    
    def _add_gru_cell(self, units_number, activation=tf.nn.relu):
        return tf.contrib.rnn.GRUCell(num_units=units_number, activation=activation)
    
    def _add_letm_cell(self, units_number, activation=tf.nn.tanh):
        return tf.contrib.rnn.LSTMCell(activation=activation, num_units=units_number)
    
    def build_feed_dict(self, input_data, return_rate, keep_prob=0.8, fee=1e-3, tao=1):
        feed = {
            self.z: return_rate,
            self.dropout_keep_prob: keep_prob,
            self.c: fee,
            self.tao: tao
        }
        for k, input_placeholder in self.model_inputs.items():
            feed[input_placeholder] = input_data[k]
        return feed
    
    def change_tao(self, feed_dict, new_tao):
        feed_dict[self.tao] = new_tao
        return feed_dict
    
    def change_drop_keep_prob(self, feed_dict, new_prob):
        feed_dict[self.dropout_keep_prob] = new_prob
        return feed_dict
    
    def get_summary(self, feed):
        return self.session.run(self.merge_op, feed_dict=feed)
    
    def train(self, feed):
        self.session.run([self.train_op], feed_dict=feed)
    
    def load_model(self, model_file='./trade_model_checkpoint'):
        self.saver.restore(self.session, model_file+'/trade_model')
    
    def save_model(self, model_path='./trade_model_checkpoint'):
        if not os.path.exists(model_path):
            os.mkdir(model_path)
        model_file = model_path + '/trade_model'
        self.saver.save(self.session, model_file)
    
    def trade(self, feed):
        rewards, cum_log_reward, cum_reward, actions = self.session.run([self.reward_t, self.cum_log_reward, self.cum_reward, self.action], feed_dict=feed)
        return rewards, cum_log_reward, cum_reward, actions
