"""scs.agent.supervisor"""

from __future__ import absolute_import, with_statement

from collections import defaultdict
from threading import Lock
from Queue import Empty

from celery.datastructures import TokenBucket
from celery.local import LocalProxy
from celery.log import SilenceRepeated
from celery.utils.timeutils import rate
from cl.common import insured as _insured
from eventlet.queue import LightQueue
from eventlet.event import Event
from kombu.syn import blocking
from kombu.utils import fxrangemax

from .signals import supervisor_ready
from .state import state
from .thread import gThread

from ..models import Node

__current = None


def insured(node, fun, *args, **kwargs):
    """Ensures any function performing a broadcast command completes
    despite intermittent connection failures."""

    def errback(exc, interval):
        supervisor.error(
            "Error while trying to broadcast %r: %r\n" % (fun, exc))
        supervisor.pause()

    return _insured(node.broker.pool, fun, args, kwargs,
                    on_revive=state.on_broker_revive,
                    errback=errback)


def ib(fun, *args, **kwargs):
    """Shortcut to ``blocking(insured(fun.im_self, fun(*args, **kwargs)))``"""
    return blocking(insured, fun.im_self, fun, *args, **kwargs)


class Supervisor(gThread):
    """The supervisor wakes up at intervals to monitor changes in the model.
    It can also be requested to perform specific operations, and these
    operations can be either async or sync.

    :keyword interval:  This is the interval (in seconds as an int/float),
       between verifying all the registered nodes.
    :keyword queue: Custom :class:`~Queue.Queue` instance used to send
        and receive commands.

    It is responsible for:

        * Stopping removed instances.
        * Starting new instances.
        * Restarting unresponsive/killed instances.
        * Making sure the instances consumes from the queues
          specified in the model, sending ``add_consumer``/-
          ``cancel_consumer`` broadcast commands to the nodes as it
          finds inconsistencies.
        * Making sure the max/min concurrency setting is as specified in the
          model,  sending ``autoscale`` broadcast commands to the noes
          as it finds inconsistencies.

    The supervisor is resilient to intermittent connection failures,
    and will autoretry any operation that is dependent on a broker.

    Since workers cannot respond to broadcast commands while the
    broker is offline, the supervisor will not restart affected
    instances until the instance has had a chance to reconnect (decided
    by the :attr:`wait_after_broker_revived` attribute).

    """
    #: Limit node restarts to 1/m, out of control nodes will be restarted.
    restart_max_rate = "1/m"

    #: Default interval_max for ensure_connection is 30 secs.
    wait_after_broker_revived = 35.0

    #: Connection errors pauses the supervisor, so events does not accumulate.
    paused = False

    #: Default interval (time in seconds as a float to reschedule).
    interval = 60.0

    def __init__(self, interval=None, queue=None, set_as_current=True):
        self.set_as_current = set_as_current
        if self.set_as_current:
            set_current(self)
        self._orig_queue_arg = queue
        self.interval = interval or self.interval
        self.queue = LightQueue() if queue is None else queue
        self._buckets = defaultdict(lambda: TokenBucket(
                                        rate(self.restart_max_rate)))
        self._pause_mutex = Lock()
        self._last_update = None
        super(Supervisor, self).__init__()
        self._rinfo = SilenceRepeated(self.info, max_iterations=30)

    def __copy__(self):
        return self.__class__(self.interval, self._orig_queue_arg)

    def pause(self):
        """Pause all timers."""
        self.respond_to_ping()
        with self._pause_mutex:
            if not self.paused:
                self.debug("pausing")
                self.paused = True

    def resume(self):
        """Resume all timers."""
        with self._pause_mutex:
            if self.paused:
                self.debug("resuming")
                self.paused = False

    def verify(self, nodes, ratelimit=False):
        """Verify the consistency of one or more nodes.

        :param nodes: List of nodes to verify.

        This operation is asynchronous, and returns a :class:`Greenlet`
        instance that can be used to wait for the operation to complete.

        """
        return self._request(nodes, self._do_verify_node,
                            {"ratelimit": ratelimit})

    def restart(self, nodes):
        """Restart one or more nodes.

        :param nodes: List of nodes to restart.

        This operation is asynchronous, and returns a :class:`Greenlet`
        instance that can be used to wait for the operation to complete.

        """
        return self._request(nodes, self._do_restart_node)

    def shutdown(self, nodes):
        """Shutdown one or more nodes.

        :param nodes: List of nodes to stop.

        This operation is asynchronous, and returns a :class:`Greenlet`
        instance that can be used to wait for the operation to complete.

        .. warning::

            Note that the supervisor will automatically restart
            any stopped nodes unless the corresponding :class:`Node`
            model has been marked as disabled.

        """
        return self._request(nodes, self._do_stop_node)

    def before(self):
        self.start_periodic_timer(self.interval, self._verify_all)

    def run(self):
        queue = self.queue
        self.info("started")
        supervisor_ready.send(sender=self)
        while not self.should_stop:
            try:
                nodes, event, action, kwargs = queue.get(timeout=1)
            except Empty:
                self.respond_to_ping()
                continue
            self.respond_to_ping()
            self._rinfo("wake-up")
            try:
                for node in nodes:
                    try:
                        action(node, **kwargs)
                    except Exception, exc:
                        self.error("Event caused exception: %r", exc)
            finally:
                event.send(True)

    def _verify_all(self, force=False):
        if self._last_update and self._last_update.ready():
            try:
                self._last_update.wait()  # collect result
            except self.GreenletExit:
                pass
            force = True
        if not self._last_update or force:
            self._last_update = self.verify(Node.objects.all(), ratelimit=True)

    def _request(self, nodes, action, kwargs={}):
        event = Event()
        self.queue.put_nowait((nodes, event, action, kwargs))
        return event

    def _verify_restart_node(self, node):
        """Restarts the node, and verifies that the node is able to start."""
        self.warn("%s node.restart" % (node, ))
        blocking(node.restart)
        is_alive = False
        for i in fxrangemax(0.1, 1, 0.4, 30):
            self.info("%s pingWithTimeout: %s", node, i)
            self.respond_to_ping()
            if insured(node, node.responds_to_ping, timeout=i):
                is_alive = True
                break
        if is_alive:
            self.warn("%s successfully restarted" % (node, ))
        else:
            self.warn("%s node doesn't respond after restart" % (
                    node, ))

    def _can_restart(self):
        """Returns true if the supervisor is allowed to restart
        nodes at this point."""
        if state.broker_last_revived is None:
            return True
        return state.time_since_broker_revived \
                > self.wait_after_broker_revived

    def _do_restart_node(self, node, ratelimit=False):
        bucket = self._buckets[node.restart]
        if ratelimit:
            if self._can_restart():
                if bucket.can_consume(1):
                    self._verify_restart_node(node)
                else:
                    self.error("%s node.disabled: Restarted too often", node)
                    node.disable()
                    self._buckets.pop(node.restart)
        else:
            self._buckets.pop(node.restart, None)
            self._verify_restart_node(node)

    def _do_stop_node(self, node):
        self.warn("%s node.shutdown" % (node, ))
        blocking(node.stop)

    def _do_verify_node(self, node, ratelimit=False):
        if not self.paused:
            if node.is_enabled and node.pk:
                if not ib(node.alive):
                    self._do_restart_node(node, ratelimit=ratelimit)
                self._verify_node_processes(node)
                self._verify_node_queues(node)
            else:
                if ib(node.alive):
                    self._do_stop_node(node)

    def _verify_node_queues(self, node):
        """Verify that the queues the node is consuming from matches
        the queues listed in the model."""
        queues = set(node.queues)
        reply = ib(node.consuming_from)
        if reply is None:
            return
        consuming_from = set(reply.keys())

        for queue in consuming_from ^ queues:
            if queue in queues:
                self.warn("%s: node.consume_from: %s" % (node, queue))
                ib(node.add_queue, queue)
            elif queue == node.direct_queue:
                pass
            else:
                self.warn("%s: node.cancel_consume: %s" % (node, queue))
                ib(node.cancel_queue, queue)

    def _verify_node_processes(self, node):
        """Verify that the max/min concurrency settings of the
        node matches that which is specified in the model."""
        max, min = node.max_concurrency, node.min_concurrency
        try:
            current = insured(node, node.stats)["autoscaler"]
        except (TypeError, KeyError):
            return
        if max != current["max"] or min != current["min"]:
            self.warn("%s: node.set_autoscale max=%r min=%r" % (
                node, max, min))
            ib(node.autoscale, max, min)


def set_current(sup):
    global __current
    __current = sup
    return __current


def get_current():
    if __current is None:
        set_current(Supervisor())
    return __current

supervisor = LocalProxy(get_current)