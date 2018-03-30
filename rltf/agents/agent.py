import logging
import threading

import tensorflow as tf

from rltf.conf        import STATS_LOGGER_NAME
from rltf.envs.utils  import get_env_monitor

stats_logger  = logging.getLogger(STATS_LOGGER_NAME)
logger        = logging.getLogger(__name__)


class Agent:
  """Base class for a Reinforcement Learning agent"""

  def __init__(self,
               env,
               train_freq,
               start_train,
               stop_step,
               eval_freq,
               eval_len,
               batch_size,
               model_dir,
               log_freq=10000,
               save_freq=100000,
              ):
    """
    Args:
      env: gym.Env. Environment in which the model will be trained.
      train_freq: int. How many environment actions to take between every 2 learning steps
      start_train: int. Time step at which the agent starts learning
      stop_step: int. Training step at which learning stops
      eval_freq: int. How many agent steps to take between every 2 evaluation runs
      eval_len: int. How many agent steps an evaluation run lasts. `<=0` means no evaluation
      batch_size: int. Batch size for training the model
      model_dir: string. Directory path for the model logs and checkpoints
      log_freq: int. Add TensorBoard summary and print progress every log_freq agent steps
      save_freq: int. Save the model every `save_freq` training steps. `<=0` means no saving
    """

    self.env            = env
    self.env_monitor    = get_env_monitor(env)

    self.batch_size     = batch_size
    self.model_dir      = model_dir
    self.save_freq      = save_freq
    self.log_freq       = log_freq

    self.start_step     = None          # Step from which the agent starts
    self.start_train    = start_train   # Step from which training starts
    self.stop_step      = stop_step     # Step at which training stops
    self.learn_started  = None          # Bool: Indicates if learning has started or not
    self.train_freq     = train_freq    # How often to run a training step

    self.eval_freq      = eval_freq     # How often to take an evaluation run
    self.eval_len       = eval_len      # How many steps to an evaluation run lasts
    self.eval_runs      = -1            # Number of evaluation runs so far

    # TensorFlow attributes
    self.model            = None
    self.summary          = None

    self.t_tf             = None
    self.t_tf_inc         = None
    self.summary_op       = None

    self.sess             = None
    self.saver            = None
    self.tb_train_writer  = None
    self.tb_eval_writer   = None


  def build(self):
    """Build the graph. If there is already a checkpoint in `self.model_dir`,
    then it will be restored instead. Calls either `self._build()` and
    `self.model.build()` or `self._restore()` and `self.model.restore()`.
    """

    # Check for checkpoint
    ckpt    = tf.train.get_checkpoint_state(self.model_dir)
    restore = ckpt and ckpt.model_checkpoint_path

    # ------------------ BUILD THE MODEL ----------------
    if not restore:
      logger.info("Building model")

      # Call the subclass _build function
      self._build()

      # Build the model
      self.model.build()

      # Create timestep variable
      with tf.device('/cpu:0'):
        self.t_tf                = tf.Variable(1, dtype=tf.int32, trainable=False, name="t_tf")
        self.t_tf_inc            = tf.assign(self.t_tf, self.t_tf + 1, name="t_inc_op")

        # Create an Op for all summaries
        self.summary_op = tf.summary.merge_all()

      # Set control variables
      self.start_step     = 1
      self.learn_started  = False

      # Create a session and initialize the model
      self.sess = self._get_sess()
      self.sess.run(tf.global_variables_initializer())
      self.sess.run(tf.local_variables_initializer())

      # Initialize the model
      self.model.initialize(self.sess)

    # ------------------ RESTORE THE MODEL ----------------
    else:
      logger.info("Restoring model")

      # Restore the graph
      ckpt_path = ckpt.model_checkpoint_path + '.meta'
      saver = tf.train.import_meta_graph(ckpt_path)
      graph = tf.get_default_graph()

      # Get the general variables and placeholders
      self.t_tf                 = graph.get_tensor_by_name("t_tf:0")
      self.t_tf_inc             = graph.get_operation_by_name("t_tf_inc")

      # Restore the model variables
      self.model.restore(graph)

      # Restore the agent subclass variables
      self._restore(graph)

      # Restore the session
      self.sess = self._get_sess()
      saver.restore(self.sess, ckpt.model_checkpoint_path)

      # Get the summary Op
      self.summary_op = graph.get_tensor_by_name("Merge/MergeSummary:0")

      # Set control variables
      self.start_step     = self.sess.run(self.t_tf)
      self.learn_started  = True

    # Create the Saver object: NOTE that you must do it after building the whole graph
    self.saver     = tf.train.Saver(max_to_keep=2, save_relative_paths=True)
    tb_dir = self.model_dir + "tb/"
    # Create TensorBoard summary writers
    self.tb_train_writer  = tf.summary.FileWriter(tb_dir + "train/", self.sess.graph)
    self.tb_eval_writer   = tf.summary.FileWriter(tb_dir + "eval/")


  def train(self):
    raise NotImplementedError()


  def eval(self):
    raise NotImplementedError()


  def reset(self):
    """This method must be called at the end of every episode. Allows for
    executing changes that stay the same for the duration of the whole episode.
    Note that it gets called both in train and eval mode
    Returns:
      obs: np.array. The result of self.env.reset()
    """
    if self.learn_started:
      self.model.reset(self.sess)
      self._reset()
    return self.env.reset()


  def close(self):
    # Close session on exit
    self.tb_train_writer.close()
    self.tb_eval_writer.close()
    self.sess.close()


  def _build(self):
    """Used by the subclass to build class specific TF objects. Must not call
    `self.model.build()`
    """
    raise NotImplementedError()


  def _reset(self):
    """Reset method to be implemented by the inheriting class"""
    raise NotImplementedError()


  def _get_feed_dict(self, t):
    """Get the placeholder parameters to feed to the model while training
    Args:
      t: int. Current timestep
    """

    raise NotImplementedError()


  def _restore(self, graph):
    """Restore the Variables, placeholders and Ops needed by the class so that
    it can operate in exactly the same way as if `self._build()` was called

    Args:
      graph: tf.Graph. Graph, restored from a checkpoint
    """
    raise NotImplementedError()


  def _define_log_info(self):
    custom_log_info = self._append_log_info()
    self.env_monitor.define_log_info(custom_log_info)


  def _append_log_info(self):
    """
    Returns:
      List of tuples `(name, format, lambda)` with information of custom subclass
      parameters to log during training. `name`: `str`, the name of the reported
      value. `modifier`: `str`, the type modifier for printing the value.
      `lambda`: A function that takes the current timestep as argument and
      returns the value to be printed.
    """
    raise NotImplementedError()


  def _append_summary(self, summary, t):
    """Append the tf.Summary that will be written to disk at timestep t with custom data.
    Used only in train mode
    Args:
      summary: tf.Summary. The summary to append
      t: int. Current time step
    """
    raise NotImplementedError()


  def _log_stats(self, t):
    """Log the training progress and append the TensorBoard summary.
    Note that the TensorBoard summary might be 1 step old.
    Args:
      t: int. Current timestep
    """

    # Log the statistics from the environment Monitor
    if t % self.log_freq == 0 and self.learn_started:
      self.env_monitor.log_stats(t)

      if self.summary:
        # Append the summary with custom data
        byte_summary = self.summary
        summary = tf.Summary()
        summary.ParseFromString(byte_summary)
        summary.value.add(tag="train/mean_ep_rew", simple_value=self.env_monitor.get_mean_ep_rew())
        self._append_summary(summary, t)

        # Log with TensorBoard
        self.tb_train_writer.add_summary(summary, global_step=t)


  def save(self):
    if self.learn_started:
      logger.info("Saving the TF model and stats")

      # Save the monitor statistics
      self.env_monitor.save()

      # Save the model
      self.saver.save(self.sess, self.model_dir, global_step=self.t_tf)

      logger.info("Save finished successfully")


  def _get_sess(self):
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    return tf.Session(config=config)



class OffPolicyAgent(Agent):
  """The base class for Off-policy agents

  Allows to run env actions and train the model in separate threads, while
  providing an easy way to synchronize between the threads. Can speed up
  training by 20-50%
  """

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

    # Create synchronization events
    self._act_chosen = threading.Event()
    self._train_done = threading.Event()


  def train(self):

    self._act_chosen.clear()
    self._train_done.set()

    env_thread  = threading.Thread(name='env_thread', target=self._run_env)
    nn_thread   = threading.Thread(name='net_thread', target=self._train_model)

    nn_thread.start()
    env_thread.start()

    # Wait for threads
    env_thread.join()
    nn_thread.join()


  def eval(self):

    logger.info("Starting evaluation")

    # Set the monitor in evaluation mode
    self.env_monitor.mode = 'e'
    self.eval_runs +=1

    start_step  = self.eval_len * self.eval_runs + 1
    stop_step   = self.eval_len + start_step

    obs = self.env.reset()

    for t in range (start_step, stop_step):

      action = self._action_eval(obs, t)
      next_obs, _, done, _ = self.env.step(action)

      # Reset the environment if end of episode
      if done:
        next_obs = self.reset()
      obs = next_obs

      if t % self.log_freq == 0:
        # Log the statistics
        self.env_monitor.log_stats(t)

        # Add a TB summary
        summary = tf.Summary()
        summary.value.add(tag="eval/mean_ep_rew", simple_value=self.env_monitor.get_mean_ep_rew())
        self.tb_eval_writer.add_summary(summary)

    # Set the monitor back to train mode
    self.env_monitor.mode = 't'

    logger.info("Evaluation finished")


  def _action_train(self, state, t):
    """Return action selected by the agent for a training step
    Args:
      state: np.array. Current state
      t: int. Current timestep
    """
    raise NotImplementedError()


  def _action_eval(self, state, t):
    """Return action selected by the agent for an evaluation step
    Args:
      state: np.array. Current state
      t: int. Current timestep
    """
    raise NotImplementedError()


  def _run_env(self):
    """Thread for running the environment. Must call `self._wait_train_done()`
    before selcting an action (by running the model). This ensures that the
    `self._train_model()` thread has finished the training step. After action
    is selected, it must call `self._signal_act_chosen()` to allow
    `self._train_model()` thread to start a new training step
    """

    obs = self.reset()

    for t in range (self.start_step, self.stop_step+1):
      # sess.run(t_inc_op)

      # Wait until net_thread is done
      self._wait_train_done()

      # Stop and run evaluation procedure
      if self.eval_len > 0 and t % self.eval_freq == 0:
        self.eval()

      # Get an action to run
      if self.learn_started:
        action = self._action_train(obs, t)

      # Choose random action if learning has not started
      else:
        action = self.env.action_space.sample()

      # Signal to net_thread that action is chosen
      self._signal_act_chosen()

      # Increment the TF timestep variable
      self.sess.run(self.t_tf_inc)

      # Run action
      next_obs, reward, done, _ = self.env.step(action)

      # Store the effect of the action taken upon obs
      self.replay_buf.store(obs, action, reward, done)

      # Reset the environment if end of episode
      if done:
        next_obs = self.reset()
      obs = next_obs

      self._log_stats(t)


  def _train_model(self):
    """Thread for trianing the model. Must call `self._wait_act_chosen()`
    before trying to run a training step on the model. This ensures that the
    `self._run_env()` thread has finished selcting an action (by running the model).
    After training step is done, it must call `self._signal_train_done()` to allow
    `self._run_env()` thread to select a new action
    """

    for t in range (self.start_step, self.stop_step+1):

      if (t >= self.start_train and t % self.train_freq == 0):

        self.learn_started = True

        # Compose feed_dict
        feed_dict = self._get_feed_dict(t)

        self._wait_act_chosen()

        # Run a training step
        self.summary, _ = self.sess.run([self.summary_op, self.model.train_op], feed_dict=feed_dict)

        # Update target network
        if t % self.update_target_freq == 0:
          self.sess.run(self.model.update_target)

      else:
        self._wait_act_chosen()

      if self.save_freq > 0 and t % self.save_freq == 0:
        self.save()

      self._signal_train_done()


  def _wait_act_chosen(self):
    # Wait until an action is chosen to be run
    while not self._act_chosen.is_set():
      self._act_chosen.wait()
    self._act_chosen.clear()

  def _wait_train_done(self):
    # Wait until training step is done
    while not self._train_done.is_set():
      self._train_done.wait()
    self._train_done.clear()

  def _signal_act_chosen(self):
    # Signal that the action is chosen and the TF graph is safe to be run
    self._act_chosen.set()

  def _signal_train_done(self):
    # Signal to main that the training step is done running
    self._train_done.set()
