"""Simple bidirectional TCP proxy for inspecting Flashforge port 8899 traffic."""

from __future__ import annotations

import argparse
import datetime
import socket
import threading


def format_preview(data: bytes) -> str:
    if len(data) <= 96 and all(32 <= b < 127 or b in (9, 10, 13) for b in data):
        return f"TEXT {data!r}"
    head = data[:32].hex(" ")
    tail = data[-16:].hex(" ") if len(data) > 48 else ""
    if tail:
        return f"BIN head=[{head}] ... tail=[{tail}]"
    return f"BIN [{head}]"


def relay(
    src: socket.socket,
    dst: socket.socket,
    label: str,
    conn_id: int,
    verbose_all: bool,
) -> None:
    count = 0
    m28_packets_left = 0
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
            count += 1
            now = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            if label == "C->P" and data.startswith(b"~M28 "):
                m28_packets_left = 5
            if (
                verbose_all
                or m28_packets_left > 0
                or count <= 10
                or len(data) < 512
                or count % 100 == 0
            ):
                print(
                    f"[{now}] #{conn_id} {label} ({len(data)} bytes): {format_preview(data)}",
                    flush=True,
                )
                if m28_packets_left > 0 and not data.startswith(b"~M28 "):
                    m28_packets_left -= 1
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle_client(
    client_sock: socket.socket,
    client_addr: tuple[str, int],
    target_host: str,
    target_port: int,
    conn_id: int,
    verbose_all: bool,
) -> None:
    print(f"[+] #{conn_id} Connected from {client_addr[0]}:{client_addr[1]}", flush=True)
    try:
        printer_sock = socket.create_connection((target_host, target_port), timeout=10.0)
        printer_sock.settimeout(None)
    except OSError as exc:
        print(f"[!] #{conn_id} Failed to connect to {target_host}:{target_port}: {exc}", flush=True)
        client_sock.close()
        return

    t1 = threading.Thread(
        target=relay,
        args=(client_sock, printer_sock, "C->P", conn_id, verbose_all),
        daemon=True,
    )
    t2 = threading.Thread(
        target=relay,
        args=(printer_sock, client_sock, "P->C", conn_id, verbose_all),
        daemon=True,
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    client_sock.close()
    printer_sock.close()
    print(f"[-] #{conn_id} Closed", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCP proxy for Flashforge 8899 inspection")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8899)
    parser.add_argument("--target-host", default="192.168.1.25")
    parser.add_argument("--target-port", type=int, default=8899)
    parser.add_argument("--verbose-all", action="store_true", help="Log every chunk")
    args = parser.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.listen_host, args.listen_port))
    srv.listen(16)
    print(
        f"Listening on {args.listen_host}:{args.listen_port} -> {args.target_host}:{args.target_port}",
        flush=True,
    )

    conn_id = 0
    try:
        while True:
            client_sock, client_addr = srv.accept()
            conn_id += 1
            threading.Thread(
                target=handle_client,
                args=(
                    client_sock,
                    client_addr,
                    args.target_host,
                    args.target_port,
                    conn_id,
                    args.verbose_all,
                ),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        srv.close()


if __name__ == "__main__":
    main()
