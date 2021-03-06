"""
LieGr - Lie Groups for unsupervised word and sentence embeddings
Implementation by Sean A. Cantrell and Robin Tully
"""
import tensorflow as tf
from tensorflow.contrib.tensorboard.plugins import projector
from tensorflow.python.training import optimizer
import os
import shutil
import numpy as np
import math
from scipy import linalg
from collections import Counter
from nltk.tokenize import TweetTokenizer
import itertools
from tqdm import tqdm
import json
import csv

pi = math.acos(-1)

twtk = TweetTokenizer(strip_handles=True, reduce_len=True)

class liegr:
    '''LieGr embeddings represent words as elements of the special orthonogal
    group SO(n).  Since each element of SO(n) is generated by an associated element
    of the Lie algebra so(n), we seek to discover the element of the algebra
    as it distills the number of free parameters to its minimum.

    We must define the set of generators for the Lie algebra, denoted here by T,
    and the structure constants, denoted here by gamma. This is done in the initialization.
    A tensor constant is also defined, denoted tfT, for select for calculations
    that do not play well with numpy arrays.

    ---------------------------------------------------------------------------

    Arguments:
        n -- the dimension of the fundamental representation of the group
        threshold -- the threshold occurrence of a word to be considered relevant
        window_size -- the size of a window to be conidered for context
        corpora -- a list of strings.  This should be a set of sentences
    '''
    def __init__(self, n, threshold=0, window_size = 5, corpora = None):
        self.N = int(n*(n-1)/2.)# This is the dimension of the so(d) Lie algebra
        self.n = n              # d=n in so(n) here, so that the group is SO(d)
        self.epsilon = 1e-5     # Threshold probability used to prevent logs from blowing up
        self.freq_threshold = threshold # Threshold occurrence of a word to be considered relevant
        self.learning_rate = 1e-3  # Optimizer learning rate
        self.batch_size = 32
        self.worse_max = 2

        ## Define the generators of the SO(n) group
        ## Idea: find the highest row for which the sum of elements of all
        ## preceeding rows is still less than the first index of T.
        ## This is the value of the row for which the matrix element is non-zero.
        ## The column value for which the matrix element is non-zero is the
        ## column passed the diagonal such that the sum of previous rows
        ## + the distance of the column from the diagonal is the first index of
        ## T.
        self.T = np.zeros([self.N,self.n,self.n])
        for I in range(self.N):
            i = int(math.ceil(-0.5+n-np.sqrt((n-0.5)**2 - 2*(I+1))) - 1.)
            dist = (I+1.) - i*(2*n-i-1)/2
            j = int(i+dist)
            self.T[I][i,j] = (-1.)**(i+j+1.)
            self.T[I][j,i] = -self.T[I][i,j]
        self.T = np.float32(self.T)

        ## Define the structure constants for the generating so(d) algebra
        ## This reverse engineers the generators.
        ## Constructed such that levi[I,J,K] = 1 iff [T_I,T_J] = T_K
        ## with the positive permutation requirement built in.
        self.gamma = np.zeros([self.N,self.N,self.N])
        for I in range(self.N):
            for J in range(self.N)[I+1:]:
                iI = int(math.ceil(-0.5+n-np.sqrt((n-0.5)**2 - 2*(I+1))) - 1.)
                dist = (I+1.) - iI*(2*n-iI-1)/2
                jI = int(iI+dist)

                iJ = int(math.ceil(-0.5+n-np.sqrt((n-0.5)**2 - 2*(J+1))) - 1.)
                dist = (J+1.) - iJ*(2*n-iJ-1)/2
                jJ = int(iJ+dist)
                if (iI == iJ) & (jI == jJ):
                    continue
                if (iI == iJ):
                    K = (jJ - jI + int( jI*(n - 0.5*(jI+1)) ) - 1)%self.N
                    self.gamma[I,J,K] = 1.
                    self.gamma[J,I,K] = -1.
                if (jI == jJ):
                    K = (iJ - iI + int( iI*(n - 0.5*(iI+1)) ) - 1)%self.N
                    self.gamma[I,J,K] = 1.
                    self.gamma[J,I,K] = -1.
                if (jI == iJ):
                    K = (jJ - iI + int( iI*(n - 0.5*(iI+1)) ) - 1)%self.N
                    self.gamma[I,J,K] = -1.
                    self.gamma[J,I,K] = 1.
                if (iI == jJ):
                    K = (iJ - jI + int( jI*(n - 0.5*(jI+1)) ) - 1)%self.N
                    self.gamma[I,J,K] = -1.
                    self.gamma[J,I,K] = 1.
        self.gamma=np.float32(self.gamma)
        self.infinite_iterator = (i for i in itertools.count(1)) ## Used to ensure gradient ops are uniquely named
        print("Building vocab.")
        self.build_vocab(corpora)
        print("Building coocurrences")
        self.build_cooccur(corpora, window_size = window_size)
        


    ## Work tokenize and convert letters to lower case
    def to_tokens(self, text):
        tokens = [str(token).lower() for token in twtk.tokenize(text) if
                  len(token)>0]
        return tokens

    ## Determine the set of unique words and their occurrence frequency
    def build_vocab(self, corpora):
        self.vocab_count = Counter()
        for doc in tqdm(corpora):
            self.vocab_count.update(set(self.to_tokens(doc)))
        ## Only keep words above a sufficient frequency (mispellings shouldn't count)
        self.vocab = {word : (i,freq) for i, (word,freq) in
                      enumerate(self.vocab_count.items()) if freq>=self.freq_threshold}
        ## Number of words in corpus
        self.token_count = np.sum([elem[1] for elem in self.vocab.values()])
        print("\nTotal vocab:", len(self.vocab))
        print("Number of words in corpus:", self.token_count)
        self.vocab_size = len(self.vocab)  ## As named.
        ## Create a dictionary that indexes the words and admits padding
        self.word_id = {word:i for i, word in enumerate(self.vocab.keys())}
        ## Create a reverse dictionary that converts indices to words
        self.id_word = {i:word for word,i in self.word_id.items()}
        ## Create a dictionary that returns the word count given a word
        self.id_count = {self.word_id[word] : count for word, (_,count) in self.vocab.items()}
        self.corpora = corpora

    def chunks(self, l, n):
        """Yield successive n-sized chunks from l."""
        for i in range(0, len(l), n):
            yield l[i:i + n]

    ## As named.
    def build_cooccur(self, corpora, window_size = 5):
        print("Creating set of windows.")
        corpora_tokens = (self.to_tokens(corpus) for corpus in corpora)
        windows = [' '.join(window) for doc in corpora_tokens for window in self.chunks(doc, window_size)]

        ## Initialize the left and right co-occurrence matrices; add 1 to vocab size for padding
        self.cooccur = np.zeros([self.vocab_size,self.vocab_size], dtype=np.float32)
        ## Initialize the occurrence vector for the words
        self.occur = np.zeros(self.vocab_size, dtype=np.float32)

        print("Tabulating co-occurrences of words.")
        for window in tqdm(windows):
            tokens = self.to_tokens(window)
            token_ids = [self.word_id[word] for word in tokens if word in self.word_id.keys()]
            # pair_ids = [(token_ids[i], v) for i, v in enumerate(token_ids[1:])]
            for i,word in enumerate(token_ids):
                self.occur[word] += 1
                for coword in token_ids[i+1:]:
                    self.cooccur[word,coword] += 1
                    self.cooccur[coword,word] += 1

            
        print("Computing co-occurrence probabilities.")
        self.prob = np.array([vec/np.sum(vec) for vec in self.cooccur])

        ## Get non-zero elements of co-occurrence probabilities
        nons = self.cooccur.nonzero()
        # nons_pair = self.pair_cooccur.nonzero()
        ## Zip them into a 1-d list
        self.nonzero = [[i,j] for i,j in zip(nons[0],nons[1])]  ## Set pairs of word IDs for pairs of words with non-zero cooccurrence
        self.index_pairs = [(i,j) for i,j in self.nonzero if j>i]


        ## Determine how many degrees of freedom are left over after nontrvial constraints are imposed
        self.remaining_DoF = self.vocab_size * self.N - len(self.index_pairs) ## Remaining degrees of freedom the word embeddings will have
        print("Total DoF:", self.vocab_size * self.N)
        print("Number of constraints:", len(self.index_pairs))
            

        self.index_pairs = np.array(self.index_pairs)

        self.p = np.array([(self.cooccur[i,j] / max([np.sqrt(self.occur[i] * self.occur[j]),self.epsilon])) ** 2
                           for i, j in self.index_pairs])

    ###########################################################################
    ## Define the gradient functions to pass to py_func
    ###########################################################################
    def _exp_grad(self,op,grad):
        deltaq = 1e-4
        ## Get raw IO
        x_raw = op.inputs[0]
        y = op.outputs[0]
        ## Convert raw input into algebra
        x = tf.einsum('ijk,ljk->il',x_raw,tf.constant(self.T))/2.
        b = tf.einsum('ij,jkl->ikl', x, tf.constant(self.gamma)) + deltaq * tf.eye(self.N)
        c = (tf.eye(self.N) - tf.linalg.expm(-b))
        s = tf.matmul(c,tf.matrix_inverse(b))
        t = tf.einsum('ijk,klm->ijlm',s,tf.constant(self.T))
        grad = tf.einsum('ijk,ilkm->iljm',y,t)
        out_vec = tf.reduce_sum(tf.reduce_sum(grad,axis=2),axis=2)
        out = tf.einsum('ij,jkl->ikl',out_vec,tf.convert_to_tensor(self.T))
        return out


    ###########################################################################
    ## Define the matrix exp to take a vector and contract appropriate gens.
    ###########################################################################
    ## Fundamental so(d) generators
    def expm(self, alg):
        # Need to generate a unique name for the gradient op to avoid duplicates:
        alg_expm = 'AlgebraExponential' + str(np.random.uniform(1,1e8)) + 'h' + str(next(self.infinite_iterator))
        
        tf.RegisterGradient(alg_expm)(self._exp_grad)
        g = tf.get_default_graph()
        
        x = tf.einsum('ij,jkl->ikl',alg,tf.constant(self.T))
        with g.gradient_override_map({'MatrixExponential': alg_expm}):
            out = tf.linalg.expm(x)
            return out
        

    ###########################################################################
    ## Make the LieGr word embeddings
    ###########################################################################
    def make_embeddings(self, name):
        ## Check if model name is given
        if (name is None) or (type(name) != str):
            return "Please enter name of embedding model."

        if not os.path.exists('./variables/' + name):
            os.mkdir('./variables/' + name)
        else:
            shutil.rmtree('./variables/' + name + '/', ignore_errors = True)
            os.mkdir('./variables/' + name)

        tf.reset_default_graph()
        #######################################################################
        ### Create the word variables, and exponentiate them
        #######################################################################
        print("\nAssigning word embeddings as TensorFlow variables.")
        init_random = tf.random_uniform([self.vocab_size, self.n, self.n])
        init_ortho = tf.qr(init_random)[0]
        with tf.variable_scope('word_embeddings'):
            self.embedding_grp = tf.get_variable(name = 'embedding_group',
                                                 initializer = init_ortho)


        ## Save the variables for TensorBoard use
        with open('./variables/' + name + '/metadata.tsv','w',encoding='utf-8') as file:
            writer = csv.writer(file)
            for i in range(1,len(self.id_word)):
                writer.writerow([self.id_word[i]])



        config = projector.ProjectorConfig()
        embedding = config.embeddings.add()
        embedding.tensor_name = self.embedding_grp.name
        embedding.metadata_path = 'metadata.tsv'
            

        #######################################################################
        ### Construct the queue
        #######################################################################
        print("Constructing queues.")
        ## Make the queue
        tot_batches = int(math.ceil(len(self.p)/self.batch_size)) #No. of batches in queue
        tq = tf.FIFOQueue(len(self.p),
                                   dtypes = [tf.int32, tf.int32,
                                             tf.float32, tf.float32],
                                   shapes = [[2],[2],
                                             [],[]])
        enq = tq.enqueue_many([self.index_pairs, self.index_pairs,
                               self.p, self.p])
        pairs_indices, pairs_indices_eval, similarity, similarity_eval = tq.dequeue_many(self.batch_size)
        pairs = tf.nn.embedding_lookup(self.embedding_grp, pairs_indices)
        pairs_eval = tf.nn.embedding_lookup(self.embedding_grp, pairs_indices_eval)
        ## Make the queue runner
        qr = tf.train.QueueRunner(tq, [enq])


        #######################################################################
        ### Construct the loss function and optimizer
        #######################################################################
        print("Constructing loss function.")
        trace = tf.square(tf.einsum('ijk,ijk->i', pairs[:,0],
                                    pairs[:,1])/self.n)
        trace_eval = tf.square(tf.einsum('ijk,ijk->i', pairs_eval[:,0],
                                         pairs_eval[:,1])/self.n)
        
        ## Loss is % loss
        loss = tf.reduce_sum(tf.abs(trace - similarity))
        loss_eval = tf.reduce_sum(tf.abs(trace_eval - similarity_eval))

        ## Set up the optimizer.
        print('Setting up the optimizer.')
        train = OrthoOptimizer(self.learning_rate).minimize(loss)

        #######################################################################
        ### Training step
        #######################################################################
        tf.summary.scalar("Embedding_Loss", loss)

        merged_summary = tf.summary.merge_all()
        # Ops to write logs to Tensorboard
        summary_writer = tf.summary.FileWriter('./variables/' + name,
                                               graph=tf.get_default_graph())
        projector.visualize_embeddings(summary_writer, config)

        with tf.Session(config=tf.ConfigProto(allow_soft_placement=True,log_device_placement=True)) as sess:
            print("\nInitializing variables.")
            sess.run(tf.global_variables_initializer())

            coord = tf.train.Coordinator()
            threads = qr.create_threads(sess, coord, start = True)

            saver = tf.train.Saver(tf.trainable_variables('word_embeddings'))

            new_loss = float('inf')
            old_loss = new_loss
            best_loss = new_loss

            best_var = sess.run([elem for elem in tf.trainable_variables('word_embeddings')])

            ## Create stopping condition if fit fails to improve after 1 epoch
            consec_worse = 0
            continual_threshold = len(self.p)
            
            ## Train over each pair once; add loss after training to list
            loss_list = []
            for _ in range(tot_batches):
                sess.run(train)
                loss_list.append(sess.run(loss_eval))
            ## Update summary and compute total loss from list
            summary = sess.run(merged_summary)
            summary_writer.add_summary(summary)
            new_loss = sum(loss_list)/(tot_batches * self.batch_size)
            print('Initial loss {}'.format(new_loss))
            
            ## Train on each batch, replacing old loss list elements
            ## to get new total loss. Require loss < ~3% error
            iterator = itertools.cycle(range(tot_batches))
            while new_loss > 0.008:
                sess.run(train)
                loss_list[next(iterator)] = sess.run(loss_eval)
                new_loss = sum(loss_list)/(tot_batches * self.batch_size)
                print('Continual Loss {}'.format(new_loss))
                if new_loss < best_loss:
                    best_var = sess.run([elem for elem in tf.trainable_variables('word_embeddings')])
                    best_loss = new_loss
                    consec_worse = 0
                if new_loss/old_loss - 1. < 0.:
                    consec_worse += 1
                if consec_worse > continual_threshold:
                    print('Continual threshold reached')
                    break


            [sess.run(tf.assign(elem,best_var[i])) for i,elem in enumerate(tf.trainable_variables('word_embeddings'))]
            saver.save(sess, './variables/' + name + '/liegr.ckpt')
            self.grp_matrix = sess.run(self.embedding_grp)
            alg_set = [np.real(linalg.logm(grp,False)[0]) for grp in self.grp_matrix]
            self.alg_matrix = np.array([np.einsum('ij,kij->k',mat,self.T)/2. for mat in alg_set])
            self.word_dict = {self.id_word[i] : vec.tolist() for
                              i,vec in enumerate(self.alg_matrix)}
            self.word_dict_mat = {self.id_word[i] : mat.tolist() for i,mat in enumerate(self.grp_matrix)}
            
            with open('./variables/' + name + '/liegr_alg.json','w') as file:
                json.dump(self.word_dict, file)
            with open('./variables/' + name + '/liegr_mat.json','w') as file:
                json.dump(self.word_dict_mat, file)
            with open('./variables/' + name + '/alg_matrix.json','w') as file:
                json.dump(self.alg_matrix.tolist(), file)
            with open('./variables/' + name + '/grp_matrix.json','w') as file:
                json.dump(self.grp_matrix.tolist(), file)

            coord.request_stop()
            coord.join(threads)

            print("\nTraining Complete.")
            sess.close()

###############################################################################
''' Optimizer '''
###############################################################################
class OrthoOptimizer(optimizer.Optimizer):
    def __init__(self, learning_rate=0.001, name="Ortho"):
        """Construct a new Orthogonal optimizer.
        
        The update rule for `variable` with gradient `g` is simply standard
        gradient descent (momentum-less) with an orthogonality constraint:
        '''
        g -> v.(v^T . g - g^T . v)
        v -> v.(I-lr*g/2).(I+lr*g/2)^-1
        '''
    
        Args:
          learning_rate: A Tensor or a floating point value.  The learning rate.
          name: Optional name for the operations created when applying gradients.
            Defaults to "Ortho".
        """
        super(OrthoOptimizer, self).__init__(False, name)
        self._lr = learning_rate
    
    def get_grads(self, loss, var_list=None):
        g,v = self.compute_gradients(loss, var_list)[0]
        var = tf.gather(v,g.indices)
        grad = g.values
        
        part1 = tf.einsum('ijk,ijl->ikl',var,grad)
        part2 = tf.einsum('ijk,ijl->ikl',grad,var)
        mod = part1 - part2
                
        return (mod, var, v, g.indices)
    
    def apply_grads(self, grad_and_vars):
        grad, var, v, indices = grad_and_vars
        I = tf.eye(v.shape.as_list()[1])
        
        mul1 = I - self._lr * grad / 2.
        mul2 = tf.matrix_inverse(I + self._lr * grad / 2)
        mul = tf.matmul(mul1,mul2)
        update = tf.matmul(var,mul)
        
        assignment = tf.scatter_update(v, indices, update, name = 'train_op', use_locking=False)
        train_op = tf.assign(v,assignment)
        return train_op
    
    def minimize(self, loss):
        grad_and_vars = self.get_grads(loss)
        train_op = self.apply_grads(grad_and_vars)
        return train_op