# @Author:      HgS_1217_
# @Create Date: 2017/12/23

# @Author:      HgS_1217_
# @Create Date: 2017/12/20

import tensorflow as tf
import numpy as np
import random
import math
import time
import cv2
from cnn.network_utils import variable_with_weight_decay, add_loss_summaries, per_class_acc, get_hist, \
    print_hist_summery
from config import CKPT_PATH


def msra_initializer(ksize, filter_num):
    stddev = math.sqrt(2. / (ksize ** 2 * filter_num))
    return tf.truncated_normal_initializer(stddev=stddev)


def orthogonal_initializer(scale=1.1):
    """
    From Lasagne and Keras. Reference: Saxe et al., http://arxiv.org/abs/1312.6120
    """
    def _initializer(shape, dtype=tf.float32, partition_info=None):
        flat_shape = (shape[0], np.prod(shape[1:]))
        a = np.random.normal(0.0, 1.0, flat_shape)
        u, _, v = np.linalg.svd(a, full_matrices=False)
        q = u if u.shape == flat_shape else v
        q = q.reshape(shape)
        return tf.constant(scale * q[:shape[0], :shape[1]], dtype=tf.float32)

    return _initializer


def get_deconv_filter(shape):
    width, height = shape[0], shape[1]
    f = math.ceil(width / 2.0)
    c = (2 * f - 1 - f % 2) / (2.0 * f)
    bilinear = np.zeros([shape[0], shape[1]])
    for x in range(width):
        for y in range(height):
            value = (1 - abs(x / f - c)) * (1 - abs(y / f - c))
            bilinear[x, y] = value
    weights = np.zeros(shape)
    for i in range(shape[2]):
        weights[:, :, i, i] = bilinear

    return tf.get_variable(name="up_filter", initializer=tf.constant_initializer(value=weights, dtype=tf.float32),
                           shape=weights.shape)


def weighted_loss(lgts, lbs, num_classes):
    with tf.name_scope('loss'):
        logits = tf.reshape(lgts, (-1, num_classes))
        labels = tf.cast(lbs, tf.int32)
        epsilon = tf.constant(value=1e-10)

        logits = logits + epsilon
        label_flat = tf.reshape(labels, (-1, 1))
        labels = tf.reshape(tf.one_hot(label_flat, depth=num_classes), (-1, num_classes))

        cross_entropy = -tf.reduce_sum(tf.multiply(labels * tf.log(tf.nn.softmax(logits) + epsilon),
                                                   np.array([0.625, 2.5])), axis=[1])
        cross_entropy_mean = tf.reduce_mean(cross_entropy, name='cross_entropy')

        tf.add_to_collection('losses', cross_entropy_mean)
        return tf.add_n(tf.get_collection('losses'), name='total_loss')


def conv2d(x, w, stride, padding='SAME'):
    return tf.nn.conv2d(x, w, strides=[1, stride, stride, 1], padding=padding)


def batch_norm_layer(x, is_training, scope):
    return tf.cond(is_training,
                   lambda: tf.contrib.layers.batch_norm(x, is_training=True, center=False,
                                                        updates_collections=None, scope=scope + "_bn"),
                   lambda: tf.contrib.layers.batch_norm(x, is_training=False, updates_collections=None,
                                                        center=False, scope=scope + "_bn", reuse=True))


def deconv_layer(x, ksize, channels, output_shape, stride=2, name=None):
    with tf.variable_scope(name):
        weights = get_deconv_filter([ksize, ksize, channels, channels])
        return tf.nn.conv2d_transpose(x, weights, output_shape, strides=[1, stride, stride, 1],
                                      padding='SAME')


def conv_layer(x, ksize, stride, feature_num, is_training, name=None, padding="SAME", relu_flag=True,
               in_channel=None):
    channel = int(x.get_shape()[-1]) if not in_channel else in_channel
    with tf.variable_scope(name) as scope:
        w = variable_with_weight_decay('w', shape=[ksize, ksize, channel, feature_num],
                                       initializer=orthogonal_initializer(), wd=None)
        b = tf.get_variable("b", [feature_num], initializer=tf.constant_initializer(0.0))
        bias = tf.nn.bias_add(conv2d(x, w, stride, padding), b)
        norm = batch_norm_layer(bias, is_training, scope.name)
        return tf.nn.relu(norm) if relu_flag else norm


def max_pool_layer(x, ksize, stride, name, padding="SAME"):
    return tf.nn.max_pool_with_argmax(x, ksize=[1, ksize, ksize, 1], strides=[1, stride, stride, 1],
                                      padding=padding, name=name)


def norm_layer(x, lsize, bias=1.0, alpha=1e-4, beta=0.75, name=None):
    return tf.nn.lrn(x, lsize, bias=bias, alpha=alpha, beta=beta, name=name)


class SegNet:
    def __init__(self, raws, labels, test_raws, test_labels, input_size=256, keep_pb=0.5, num_classes=2,
                 batch_size=100, epoch_size=100, learning_rate=0.001):
        """
        :param raws: path list of raw images
        :param labels: path list of labels
        :param test_raws: path list of test images
        :param test_labels: path list of test labels
        :param keep_pb: keep probability of dropout
        :param num_classes: number of result classes
        """
        self.raws = raws
        self.labels = labels
        self.test_raws = test_raws
        self.test_labels = test_labels
        self.keep_pb = keep_pb
        self.num_classes = num_classes
        self.batch_size = batch_size
        self.epoch_size = epoch_size
        self.learning_rate = learning_rate
        self.input_size = input_size
        self.logits = None
        self.softmax = None
        self.classes = None
        self.loss = None

        self.x = tf.placeholder(tf.float32, shape=[None, self.input_size, self.input_size, 1], name="input_x")
        self.y = tf.placeholder(tf.float32, shape=[None, self.input_size, self.input_size, 1],
                                name="input_y")
        self.is_training = tf.placeholder(tf.bool, name='is_training')
        self.width = tf.placeholder(tf.int32, name="width")

    def build_network(self, images, labels, batch_size, is_training):
        x_resh = tf.reshape(images, [-1, self.input_size, self.input_size, 1])

        norm1 = norm_layer(x_resh, 5, name="norm1")
        conv1 = conv_layer(norm1, 7, 1, 64, is_training, "conv1")
        pool1, pool1_indices = max_pool_layer(conv1, 2, 2, "pool1")

        conv2 = conv_layer(pool1, 7, 1, 64, is_training, "conv2")
        pool2, pool2_indices = max_pool_layer(conv2, 2, 2, "pool2")

        conv3 = conv_layer(pool2, 7, 1, 64, is_training, "conv3")
        pool3, pool3_indices = max_pool_layer(conv3, 2, 2, "pool3")

        conv4 = conv_layer(pool3, 7, 1, 64, is_training, "conv4")
        pool4, pool4_indices = max_pool_layer(conv4, 2, 2, "pool4")

        upsample4 = deconv_layer(pool4, 2, 64, [batch_size, 16, 16, 64], name="upsample4")
        conv_decode4 = conv_layer(upsample4, 7, 1, 64, is_training, "conv_decode4", relu_flag=False,
                                  in_channel=64)

        upsample3 = deconv_layer(conv_decode4, 2, 64, [batch_size, 32, 32, 64], name="upsample3")
        conv_decode3 = conv_layer(upsample3, 7, 1, 64, is_training, "conv_decode3", relu_flag=False,
                                  in_channel=64)

        upsample2 = deconv_layer(conv_decode3, 2, 64, [batch_size, 64, 64, 64], name="upsample2")
        conv_decode2 = conv_layer(upsample2, 7, 1, 64, is_training, "conv_decode2", relu_flag=False,
                                  in_channel=64)

        upsample1 = deconv_layer(conv_decode2, 2, 64, [batch_size, 128, 128, 64], name="upsample1")
        conv_decode1 = conv_layer(upsample1, 7, 1, 64, is_training, "conv_decode1", relu_flag=False,
                                  in_channel=64)

        with tf.variable_scope('conv_classifier') as scope:
            w = variable_with_weight_decay('w', shape=[1, 1, 64, self.num_classes],
                                           initializer=msra_initializer(1, 64), wd=0.0005)
            b = tf.get_variable("b", [self.num_classes], initializer=tf.constant_initializer(0.0))
            conv_classifier = tf.nn.bias_add(conv2d(conv_decode1, w, 1), b, name=scope.name)

        logits = conv_classifier
        loss = weighted_loss(conv_classifier, labels, self.num_classes)
        classes = tf.argmax(logits, axis=3)

        return loss, logits, classes

    def train_set(self, total_loss, global_step):
        loss_averages_op = add_loss_summaries(total_loss)

        with tf.control_dependencies([loss_averages_op]):
            opt = tf.train.AdamOptimizer(self.learning_rate)
            grads = opt.compute_gradients(total_loss)

        apply_gradient_op = opt.apply_gradients(grads, global_step=global_step)

        variable_averages = tf.train.ExponentialMovingAverage(0.9999, global_step)
        variables_averages_op = variable_averages.apply(tf.trainable_variables())

        with tf.control_dependencies([apply_gradient_op, variables_averages_op]):
            train_op = tf.no_op(name='train')

        return train_op

    def load_img(self, path_list, label_flag):
        if label_flag:
            return [tf.round(tf.image.convert_image_dtype(tf.image.decode_jpeg(
                tf.read_file(path), channels=1), dtype=tf.uint8)).eval() / 255 for path in path_list]
            # images = []
            # for path in path_list:
            #     bi_img = tf.round(tf.image.convert_image_dtype(tf.image.decode_jpeg(
            #         tf.read_file(path), channels=1), dtype=tf.uint8) / 255)
            #     images.append(tf.concat([1 - bi_img, bi_img], -1).eval())
            # return images
        return [tf.image.convert_image_dtype(tf.image.decode_jpeg(
            tf.read_file(path), channels=1), dtype=tf.uint8).eval() for path in path_list]

    def batch_generator(self):
        # rand_num = [0]
        rand_num = random.sample(range(len(self.raws)), self.batch_size)
        batch_raws, batch_labels = [self.raws[i] for i in rand_num], [self.labels[i] for i in rand_num]
        return self.load_img(batch_raws, False), self.load_img(batch_labels, True)

    def test_generator(self, i):
        isize = self.batch_size
        raws_test, labels_test = self.test_raws[i * isize: i * isize + isize], \
                                 self.test_labels[i * isize: i * isize + isize]
        return self.load_img(raws_test, False), self.load_img(labels_test, True)

    def train_network(self, is_finetune=False):
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True

        with tf.Session(config=config) as sess:
            global_step = tf.Variable(0, trainable=False)

            loss, eval_prediction, classes = self.build_network(self.x, self.y, self.width, self.is_training)
            train_op = self.train_set(loss, global_step)

            tf.add_to_collection('result', classes)
            saver = tf.train.Saver(tf.global_variables())

            if (is_finetune):
                saver.restore(sess, CKPT_PATH)
            else:
                sess.run(tf.global_variables_initializer())

            min_loss = 9999
            for i in range(self.epoch_size):
                batch_xs, batch_ys = self.batch_generator()
                feed_dict = {self.x: batch_xs, self.y: batch_ys, self.is_training: True,
                             self.width: len(batch_xs)}

                start_time = time.time()
                loss_batch, eval_pre, _ = sess.run([loss, eval_prediction, train_op], feed_dict=feed_dict)
                duration = time.time() - start_time
                print("train %d, loss %g, duration %.3f" % (i, loss_batch, duration))
                per_class_acc(eval_pre, batch_ys)

                if i % 10 == 9:
                    print("\nstart validating.....")
                    total_val_loss = 0.0
                    hist = np.zeros((self.num_classes, self.num_classes))
                    test_iter = 1
                    for test_step in range(test_iter):
                        x_test, y_test = self.test_generator(test_step)
                        loss_test, eval_pre = sess.run([loss, eval_prediction], feed_dict={
                            self.x: x_test,
                            self.y: y_test,
                            self.is_training: True,
                            self.width: len(x_test)
                        })
                        total_val_loss += loss_test
                        hist += get_hist(eval_pre, y_test)
                    print("val loss: ", total_val_loss / test_iter)
                    print_hist_summery(hist)
                    print("end validating....\n")

                    if loss_batch < min_loss:
                        min_loss = loss_batch
                        print("saving model....\n")
                        saver.save(sess, CKPT_PATH)

    def check(self):
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True

        with tf.Session(config=config) as sess:
            global_step = tf.Variable(0, trainable=False)

            loss, eval_prediction, classes = self.build_network(self.x, self.y, self.width, self.is_training)
            train_op = self.train_set(loss, global_step)

            saver = tf.train.Saver(tf.global_variables())
            saver.restore(sess, CKPT_PATH)

            batch_xs, batch_ys = self.batch_generator()
            feed_dict = {self.x: batch_xs, self.y: batch_ys, self.is_training: True,
                         self.width: len(batch_xs)}

            loss_batch, eval_pre, res, _ = sess.run([loss, eval_prediction, classes, train_op], feed_dict=feed_dict)
            per_class_acc(eval_pre, batch_ys)

        out = np.array(res[0]) * 255
        cv2.imwrite("D:/Computer Science/Github/Palmprint-Segmentation/cv_segment/pics/net_res2.jpg", out)
