import eventlet
import logging
import sys

from collections import defaultdict
from threading import Lock

from celery import current_app as celery
from celery.datastructures import TokenBucket
from celery.utils.timeutils import rate
from eventlet.queue import LightQueue
from eventlet.event import Event
from kombu.syn import blocking

from django.db.models import signals

from scs.models import Node
from scs.state import state
from scs.thread import gThread


logger = logging.getLogger("Supervisor")


def insured(fun, *args, **kwargs):
    """Ensures any function performing a broadcast command completes
    despite intermittent connection failures."""

    def errback(exc, interval):
        sys.stderr.write("Error while trying to broadcast %r: %r\n" % (
            fun, exc))
        supervisor.pause()

    with celery.pool.acquire(block=True) as conn:
        conn.ensure_connection(errback=errback)
        # we cache the channel for subsequent calls, this has to be
        # reset on revival.
        channel = getattr(conn, "_publisher_chan", None)

        def on_revive(channel):
            conn._publisher_chan = channel
            state.on_broker_revive(channel)
            supervisor.resume()

        insured = conn.autoretry(fun, channel, errback=errback,
                                               on_revive=on_revive)
        retval, _ = insured(*args, **dict(kwargs, connection=conn))
        return retval


class Supervisor(gThread):
    """The supervisor wakes up at intervals to monitor changes in the model.
    It can also be requested to perform specific operations, and these operations
    can be either sync or async.

    It is responsible for:

        * Stopping removed instances.
        * Starting new instances.
        * Restarting unresponsive/killed instances.
        * Making sure the instances consumes from the queues
          specified in the model, sending ``add_consumer``/-
          `cancel_consumer`` broadcast commands to the nodes as it
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
    # Limit node restarts to 1/m, out of control nodes will be restarted.
    restart_max_rate = "1/m"

    # default interval_max for ensure_connection is 30 secs.
    wait_after_broker_revived = 35.0

    # Connection errors pauses the supervisor, so events does not accumulate.
    paused = False

    def __init__(self, queue=None):
        if queue is None:
            queue = LightQueue()
        self.queue = queue
        self._buckets = defaultdict(lambda: TokenBucket(
                                    rate(self.restart_max_rate)))
        self._pause_mutex = Lock()
        super(Supervisor, self).__init__()

    def verify(self, nodes):
        """Verify the consistency of one or more nodes.

        :param nodes: List of nodes to verify.

        This operation is asynchronous, and returns a :class:`Greenlet`
        instance that can be used to wait for the operation to complete.

        """
        if not self.paused:
            return self._request(nodes, self._do_verify_node)

    def pause(self):
        with self._pause_mutex:
            self.paused = True

    def resume(self):
        with self._pause_mutex:
            self.paused = False

    def restart(self, nodes):
        """Restart one or more nodes.

        :param nodes: List of nodes to restart.

        This operation is asynchronous, and returns a :class:`Greenlet`
        instance that can be used to wait for the operation to complete.

        """
        return self._request(nodes, self._do_restart_node)

    def stop(self, nodes):
        """Stop one or more nodes.

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
        self.connect_signals()
        self.start_periodic_update()

    def run(self):
        queue = self.queue
        debug = self.debug
        self.info("started...")
        while 1:
            nodes, event, action = queue.get()
            debug("wake-up")
            try:
                for node in nodes:
                    try:
                        action(node)
                    except Exception, exc:
                        self.error("Event caused exception: %r" % (exc, ),
                                   exc_info=sys.exc_info())
            finally:
                event.send(True)

    def start_periodic_update(self, interval=5.0):
        self.verify(Node.objects.all())
        eventlet.spawn_after(interval, self.start_periodic_update,
                             interval=interval)

    def _request(self, nodes, action):
        event = Event()
        self.queue.put_nowait((nodes, event, action))
        return event

    def _verify_restart_node(self, node):
        """Restarts the node, and verifies that the node is able to start."""
        self.warn("%s node.restart" % (node, ))
        blocking(node.restart)
        is_alive = False
        for i in (0.1, 0.5, 1, 1, 1, 1):
            self.info("%s pingWithTimeout: %s" % (node, i))
            if insured(node.responds_to_ping, timeout=i):
                is_alive = True
                break
        if is_alive:
            self.warn("%s successfully restarted" % (node, ))
        else:
            self.warn("%s node doesn't respond after restart" % (
                    node, ))

    def _can_restart(self):
        """Returns true if the supervisor are allowed to restart
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
                    self.error(
                        "%s node.disabled: Restarted too many times" % (node, ))
                    node.disable()
                    self._buckets.pop(node.restart)
        else:
            self._buckets.pop(node.restart, None)
            self._verify_restart_node(node)

    def _do_stop_node(self, node):
        self.warn("%s node.shutdown" % (node, ))
        blocking(node.stop)

    def _do_verify_node(self, node):
        if node.is_enabled and node.pk:
            if not blocking(insured, node.alive):
                self._do_restart_node(node, ratelimit=True)
            self._verify_node_processes(node)
            self._verify_node_queues(node)
        else:
            if blocking(insured, node.alive):
                self._do_stop_node(node)

    def _verify_node_queues(self, node):
        """Verify that the queues the node is consuming from matches
        the queues listed in the model."""
        queues = set(queue.name for queue in node.queues.enabled())
        reply = blocking(insured, node.consuming_from)
        if reply is None:
            return
        consuming_from = set(reply.keys())

        for queue in consuming_from ^ queues:
            if queue in queues:
                self.warn("%s: node.consume_from: %s" % (node, queue))
                blocking(insured, node.add_queue, queue)
            elif queue == node.direct_queue:
                pass
            else:
                self.warn("%s: node.cancel_consume: %s" % (node, queue))
                blocking(insured, node.cancel_queue, queue)

    def _verify_node_processes(self, node):
        """Verify that the max/min concurrency settings of the
        node matches that which is specified in the model."""
        max, min = node.max_concurrency, node.min_concurrency
        try:
            current = insured(node.stats)["autoscaler"]
        except (TypeError, KeyError):
            return
        if max != current["max"] or min != current["min"]:
            self.warn("%s: node.set_autoscale max=%r min=%r" % (
                node, max, min))
            blocking(insured, node.autoscale, max, min)

    def connect_signals(self):

        def verify_on_changed(instance=None, **kwargs):
            print("VERIFY ON CHANGED")
            self.verify([instance]).wait()

        def stop_on_delete(instance=None, **kwargs):
            print("STOP ON DELETE")
            self.stop([instance]).wait()

        signals.post_save.connect(verify_on_changed)
        signals.post_delete.connect(stop_on_delete)
        signals.m2m_changed.connect(verify_on_changed)


supervisor = Supervisor()
