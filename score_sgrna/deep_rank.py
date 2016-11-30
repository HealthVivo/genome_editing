"""
Deep rank: Rank sgRNAs by Convolutional Neural Network

Now we only use sequence as input, we could consider GC content, position on
the protein, structure etc.
"""

import numpy as np
import scipy.stats
import tensorflow as tf


# Input
def generate_input(seqs, feats, score):
    dataset = []
    for i, seq in enumerate(seqs):
        encoding_seq = one_hot_encoding(seq)
        dataset.append([encoding_seq, [x[i] for x in feats], score[i]])
    return np.array(dataset)


def one_hot_encoding(seq):
    encoding_dict = {'A': 0, 'T': 1, 'C': 2, 'G': 3, 'N': -1}
    seq = seq.upper()
    encoding_mat = np.zeros(shape=(4, len(seq)))
    for i, bp in enumerate(seq):
        if bp == 'N':
            continue
        encoding_mat[encoding_dict[bp]][i] = 1
    return encoding_mat


def split_data_by_gene(dataset, test_gene):
    train_data = []
    test_data = []
    for record in dataset:
        if record[-1] == test_gene:
            test_data.append(record)
        else:
            train_data.append(record)
    train_data = np.array(train_data)
    test_data = np.array(test_data)
    return train_data, test_data


def split_data_random(dataset, train_ratio=0.6, valid_ratio=0.2,
                      test_ratio=0.2):
    assert train_ratio + valid_ratio + test_ratio == 1, 'Wrong ratio'
    total_num = dataset.shape[0]
    train_num = int(total_num * train_ratio)
    valid_num = int(total_num * valid_ratio)

    train_index = np.random.choice(total_num, train_num, replace=False)
    test_index = np.array(list(set(range(total_num)) - set(train_index)))
    train_data = dataset[train_index]
    valid_index = test_index[:valid_num]
    test_index = test_index[valid_num:]
    valid_data = dataset[valid_index]
    test_data = dataset[test_index]

    return train_data, valid_data, test_data


def permute(x, y):
    assert len(x[0]) == len(
        y), "The number of features and responses is different"
    permute_index = np.random.choice(len(y), len(y), replace=False)
    return [x[0][permute_index], x[1][permute_index]], y[permute_index]


def get_gc_content(seq):
    return (seq.count('C') + seq.count('G')) / len(seq)


def transform(input_data):
    cnn = np.array([x[0] for x in input_data])
    dnn = np.array([x[1] for x in input_data])
    response = np.array([x[2] for x in input_data])

    cnn_transform = np.reshape(cnn, (-1, 4, 30, 1))
    response_transform = np.reshape(response, (-1, 1))
    return [cnn_transform, dnn], response_transform


def generate_ms_input(ms_data):
    seqs = ms_data.loc[:, '30mer'].values
    pp = ms_data.loc[:, 'Percent Peptide'].values / 100
    gc = [get_gc_content(x[4:-6]) for x in seqs]
    feats = [pp, gc]
    rank_score = ms_data.loc[:, 'score_drug_gene_rank'].values
    return generate_input(seqs, feats, rank_score)


# Inference
def inference(cnn_input, dnn_input, keep_prob):
    with tf.name_scope('conv1'):
        kernel = weight_variable([4, 4, 1, 32])
        conv1 = tf.nn.conv2d(cnn_input, kernel, strides=[1, 1, 1, 1],
                             padding='SAME')
        biases = bias_variable([32])
        conv1_act = tf.nn.relu(conv1 + biases)
        tf.histogram_summary('conv1', conv1_act)

    # pool1 = tf.nn.max_pool(conv1_act, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1],
    #                        padding='VALID')

    with tf.name_scope('conv2'):
        kernel = weight_variable([2, 2, 32, 64])
        conv2 = tf.nn.conv2d(conv1_act, kernel, strides=[1, 1, 1, 1],
                             padding='SAME')
        biases = bias_variable([64])
        conv2_act = tf.nn.relu(conv2 + biases)

    pool2 = tf.nn.max_pool(conv2_act, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1],
                           padding='VALID')

    conv_flat_dim = int(pool2.get_shape()[1] * pool2.get_shape()[2] *
                        pool2.get_shape()[3])
    conv_flat = tf.reshape(pool2, [-1, conv_flat_dim])

    # add dnn features
    hidden1_input = tf.concat(1, [conv_flat, dnn_input])
    hidden1_dim = conv_flat_dim + int(dnn_input.get_shape()[1])
    hidden1 = fc_layer(hidden1_input, hidden1_dim, 1024, layer_name='layer1')

    with tf.name_scope('dropout'):
        dropped = tf.nn.dropout(hidden1, keep_prob)

    y = fc_layer(dropped, 1024, 1, layer_name='readout', act=tf.identity)
    y_hat = 1 / (1 + tf.exp(-y))
    return y_hat


def weight_variable(shape):
    return tf.Variable(tf.truncated_normal(shape=shape, stddev=0.1))


def bias_variable(shape):
    return tf.Variable(tf.constant(0.1, shape=shape))


def fc_layer(input_tensor, input_dim, output_dim, layer_name, act=tf.nn.relu):
    with tf.name_scope(layer_name):
        with tf.name_scope('weights'):
            w = weight_variable([input_dim, output_dim])
        with tf.name_scope('biases'):
            b = bias_variable(shape=[output_dim])
        with tf.name_scope('pre_activate'):
            preact = tf.matmul(input_tensor, w) + b
        activations = act(preact)
    return activations


def loss(y_hat, input_y):
    # y_hat = inference(input_x, keep_prob)
    cov = tf.reduce_sum(tf.matmul(tf.transpose(y_hat - tf.reduce_mean(y_hat)),
                                  input_y - tf.reduce_mean(input_y)))
    sd_y = tf.sqrt(
        tf.matmul(tf.transpose(input_y - tf.reduce_mean(input_y)),
                  input_y - tf.reduce_mean(input_y)))
    sd_y_hat = tf.sqrt(tf.matmul(tf.transpose(y_hat - tf.reduce_mean(y_hat)),
                                 y_hat - tf.reduce_mean(y_hat)))
    pearson_r = cov / (sd_y * sd_y_hat)
    cost_function = -pearson_r
    return cost_function


def train_step(cost_function, lr=1e-4,
               optimizer=tf.train.AdamOptimizer):
    step = optimizer(lr).minimize(cost_function)
    return step


def deep_rank(train_x, train_y, valid_x=None, valid_y=None,
              max_epoch=20, batch_size=100,
              model_save_path='deep_rank_model.ckpt'):
    cnn_input_height, cnn_input_width, cnn_input_channel = train_x[0][0].shape
    dnn_input_len = len(train_x[1][0])

    # early stopping parameters
    num_waiting = 3
    improve_accuracy = 0.005
    count = 0
    valid_loss_best = 0

    with tf.Graph().as_default():
        # train_x and train_y
        cnn_input = tf.placeholder(tf.float32,
                                   [None, cnn_input_height, cnn_input_width, 1])
        dnn_input = tf.placeholder(tf.float32, [None, dnn_input_len])
        y = tf.placeholder(tf.float32, [None, 1])
        keep_prob = tf.placeholder(tf.float32)

        tf.image_summary('cnn_input', cnn_input)

        print('cnn_input: {}, dnn_input: {}'.format(cnn_input.get_shape(),
                                                    dnn_input.get_shape()))

        # inference model
        y_hat = inference(cnn_input, dnn_input, keep_prob)

        # loss
        cost_function = loss(y_hat, y)
        tf.histogram_summary('cost', cost_function)

        # train_op
        train_op = train_step(cost_function)

        # init
        init = tf.initialize_all_variables()

        # saver
        saver = tf.train.Saver(tf.all_variables())

        # summary
        summary_op = tf.merge_all_summaries()

        # sess
        sess = tf.Session()
        sess.run(init)
        summary_writer = tf.train.SummaryWriter('./log', sess.graph)

        # train parameters
        batch_num = int(train_y.shape[0] / batch_size)

        # training
        for epoch in range(max_epoch):
            train_x, train_y = permute(train_x, train_y)
            for i in range(batch_num):
                batch_x_cnn = train_x[0][
                              (i * batch_size): ((i + 1) * batch_size)]
                batch_x_dnn = train_x[1][
                              (i * batch_size): ((i + 1) * batch_size)]
                batch_y = train_y[(i * batch_size): ((i + 1) * batch_size)]
                feed_dict = {cnn_input: batch_x_cnn, dnn_input: batch_x_dnn,
                             y: batch_y, keep_prob: 0.5}
                sess.run(train_op, feed_dict=feed_dict)
            if (valid_x is not None) and (valid_y is not None):
                valid_feed_dict = {cnn_input: valid_x[0], dnn_input: valid_x[1],
                                   y: valid_y, keep_prob: 1}
            else:
                valid_feed_dict = {cnn_input: train_x[0], dnn_input: train_x[1],
                                   y: train_y, keep_prob: 1}
            valid_loss, summary_str = sess.run([cost_function, summary_op],
                                               feed_dict=valid_feed_dict)
            summary_writer.add_summary(summary_str, epoch)
            print(valid_loss[0])

            if (np.abs(valid_loss) - np.abs(
                    valid_loss_best)) < improve_accuracy:
                count += 1
            else:
                count = 0

            if valid_loss < valid_loss_best:
                valid_loss_best = valid_loss
                save_path = saver.save(sess, model_save_path)
                print('Save model in {}'.format(save_path))

            if count >= num_waiting:
                break

        # save_path = saver.save(sess, model_save_path)
        # print('Save model in {}'.format(save_path))
        sess.close()


# Prediction and evaluation
def predict(model_save_path, input_x, input_y):
    cnn_input_height, cnn_input_width, cnn_input_channel = input_x[0][0].shape
    dnn_input_len = len(input_x[1][0])

    with tf.Graph().as_default():
        cnn_input = tf.placeholder(tf.float32,
                                   [None, cnn_input_height, cnn_input_width, 1])
        dnn_input = tf.placeholder(tf.float32, [None, dnn_input_len])
        y = tf.placeholder(tf.float32, [None, 1])
        keep_prob = tf.placeholder(tf.float32)
        y_hat = inference(cnn_input, dnn_input, keep_prob)
        cost_function = loss(y_hat, y)
        saver = tf.train.Saver(tf.all_variables())
        with tf.Session() as sess:
            saver.restore(sess, model_save_path)
            feed_dict = {cnn_input: input_x[0], dnn_input: input_x[1],
                         y: input_y, keep_prob: 1}
            y_pred, pearsonr = sess.run([y_hat, cost_function],
                                        feed_dict=feed_dict)
        return y_pred, pearsonr[0]


def evaluate(y_true, y_pred):
    return scipy.stats.pearsonr(y_true, y_pred)[0], \
           scipy.stats.spearmanr(y_true, y_pred)[0]
