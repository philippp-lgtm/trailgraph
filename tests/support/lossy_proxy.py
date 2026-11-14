"""A TCP proxy in front of a server that can lose the answer to chosen requests (S tests).

The request reaches the server and is answered there, but the client's connection is closed
before a byte of the answer comes back: a write that was applied and whose answer was lost.
"""

import select
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

CHUNK = 65536


@dataclass
class Proxy:
    port: int
    seen: list[str] = field(default_factory=list)  # request lines, in order
    lost: list[str] = field(default_factory=list)  # those whose answer was dropped


@contextmanager
def lossy_proxy(upstream_port: int, drop: Callable[[str, int], bool]) -> Iterator[Proxy]:
    """``drop(request line, number)``: ``number`` counts the requests with that line from 1;
    True loses the answer of that request."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    proxy = Proxy(listener.getsockname()[1])
    counts: dict[str, int] = {}
    lock = threading.Lock()
    stopped = threading.Event()

    def serve(client: socket.socket) -> None:
        upstream = socket.create_connection(("127.0.0.1", upstream_port))
        try:
            first = client.recv(CHUNK)
            if not first:
                return
            line = first.split(b"\r\n", 1)[0].decode(errors="replace")
            with lock:
                counts[line] = counts.get(line, 0) + 1
                proxy.seen.append(line)
                lose = drop(line, counts[line])
            upstream.sendall(first)
            sockets = {client: upstream, upstream: client}
            answered = False
            while True:
                ready, _, _ = select.select(list(sockets), [], [], 30)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(CHUNK)
                    if not data:
                        return
                    if source is upstream and lose and not answered:
                        with lock:
                            proxy.lost.append(line)
                        return  # the server answered; the client never hears of it
                    answered = answered or source is upstream
                    sockets[source].sendall(data)
        except OSError:
            pass
        finally:
            client.close()
            upstream.close()

    def accept() -> None:
        listener.settimeout(0.2)
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=serve, args=(client,), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield proxy
    finally:
        stopped.set()
        thread.join(timeout=5)
        listener.close()
