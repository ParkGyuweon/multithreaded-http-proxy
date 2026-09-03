"""A small thread-per-connection proxy for plain HTTP traffic.

The proxy intentionally rejects HTTPS CONNECT requests. Image filtering follows
the original toggle keywords: requesting a URL containing ``image_off`` enables
image blocking, and ``image_on`` disables it.
"""

import argparse
import socket
import threading
from dataclasses import dataclass
from typing import Iterable


BUFFER_SIZE = 64 * 1024
DEFAULT_REDIRECT_KEYWORD = "google"
DEFAULT_REDIRECT_HOST = "mnet.yonsei.ac.kr"
DEFAULT_REDIRECT_PORT = 80
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp")


@dataclass
class ProxyConfig:
    listen_port: int
    redirect_keyword: str = DEFAULT_REDIRECT_KEYWORD
    redirect_host: str = DEFAULT_REDIRECT_HOST
    redirect_port: int = DEFAULT_REDIRECT_PORT


@dataclass
class RemoteTarget:
    host: str
    port: int
    path: str
    redirected: bool = False


class SharedState:
    def __init__(self) -> None:
        self.image_filter_enabled = False
        self.request_count = 0
        self.counter_lock = threading.Lock()
        self.filter_lock = threading.Lock()
        self.print_lock = threading.Lock()

    def next_request_number(self) -> int:
        with self.counter_lock:
            self.request_count += 1
            return self.request_count

    def set_image_filter(self, enabled: bool) -> None:
        with self.filter_lock:
            self.image_filter_enabled = enabled

    def is_image_filter_enabled(self) -> bool:
        with self.filter_lock:
            return self.image_filter_enabled


def parse_headers(lines: Iterable[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.lower()] = value.strip()
    return headers


def split_host_port(host_value: str, default_port: int = 80) -> tuple[str, int]:
    if ":" not in host_value:
        return host_value, default_port

    host, port_value = host_value.rsplit(":", 1)
    try:
        return host, int(port_value)
    except ValueError:
        return host, default_port


def normalize_target_path(url: str) -> tuple[str, str]:
    value = url
    if "://" in value:
        value = value.split("://", 1)[1]

    if "/" not in value:
        return value, "/"

    host_part, path_part = value.split("/", 1)
    return host_part, "/" + path_part


def resolve_remote_target(url: str, headers: dict[str, str], config: ProxyConfig) -> RemoteTarget:
    host_part, path = normalize_target_path(url)
    host, port = split_host_port(host_part)

    if not host and "host" in headers:
        host, port = split_host_port(headers["host"])

    redirected = False
    if config.redirect_keyword and (
        config.redirect_keyword in url.lower() or config.redirect_keyword in host.lower()
    ):
        host = config.redirect_host
        port = config.redirect_port
        redirected = True

    return RemoteTarget(host=host, port=port, path=path, redirected=redirected)


def is_image_request(url: str) -> bool:
    clean_path = url.split("?", 1)[0].lower()
    return clean_path.endswith(IMAGE_EXTENSIONS)


def build_forward_request(
    method: str,
    path: str,
    http_version: str,
    raw_header_lines: list[str],
    target: RemoteTarget,
) -> bytes:
    rewritten_headers = [f"{method} {path} {http_version}"]
    saw_connection = False
    saw_host = False

    for line in raw_header_lines[1:]:
        if not line:
            continue

        lower_line = line.lower()
        if lower_line.startswith("host:"):
            saw_host = True
            rewritten_headers.append(f"Host: {target.host}")
        elif lower_line.startswith("connection:"):
            saw_connection = True
            rewritten_headers.append("Connection: close")
        elif lower_line.startswith("proxy-connection:") or lower_line.startswith("keep-alive:"):
            continue
        else:
            rewritten_headers.append(line)

    if not saw_host:
        rewritten_headers.append(f"Host: {target.host}")
    if not saw_connection:
        rewritten_headers.append("Connection: close")

    return ("\r\n".join(rewritten_headers) + "\r\n\r\n").encode("utf-8")


def receive_all(sock: socket.socket) -> bytes:
    chunks = []
    while True:
        data = sock.recv(BUFFER_SIZE)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def parse_response_metadata(response: bytes) -> tuple[str, str]:
    header_end = response.find(b"\r\n\r\n")
    header_bytes = response[:header_end] if header_end != -1 else response
    header_text = header_bytes.decode("utf-8", errors="ignore")
    lines = header_text.split("\r\n")

    status = ""
    content_type = ""
    if lines:
        parts = lines[0].split(" ", 2)
        if len(parts) >= 2:
            status = " ".join(parts[1:])

    for line in lines:
        if line.lower().startswith("content-type:"):
            content_type = line.split(":", 1)[1].strip()
            break

    return status, content_type


def not_found_response(message: str) -> bytes:
    body = message.encode("utf-8")
    headers = (
        "HTTP/1.1 404 Not Found\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("utf-8")
    return headers + body


def handle_client(client_socket: socket.socket, client_address: tuple[str, int], config: ProxyConfig, state: SharedState) -> None:
    request_number = state.next_request_number()
    log_lines: list[str] = []
    server_socket: socket.socket | None = None

    try:
        request_data = client_socket.recv(BUFFER_SIZE)
        if not request_data:
            return

        decoded_request = request_data.decode("utf-8", errors="ignore")
        header_lines = decoded_request.split("\r\n")
        request_parts = header_lines[0].split()
        if len(request_parts) < 2:
            return

        method = request_parts[0].upper()
        url = request_parts[1]
        http_version = request_parts[2] if len(request_parts) >= 3 else "HTTP/1.1"

        if method == "CONNECT":
            client_socket.sendall(not_found_response("HTTPS tunneling is not supported by this proxy."))
            return

        if "image_off" in url:
            state.set_image_filter(True)
        elif "image_on" in url:
            state.set_image_filter(False)

        headers = parse_headers(header_lines[1:])
        target = resolve_remote_target(url, headers, config)
        filter_enabled = state.is_image_filter_enabled()
        block_image = filter_enabled and is_image_request(url)

        redirect_log = "O" if target.redirected else "X"
        filter_log = "O" if filter_enabled else "X"
        user_agent = headers.get("user-agent", "")

        log_lines.extend(
            [
                "-" * 60,
                f"{request_number} [{redirect_log}] URL redirection [{filter_log}] Image filter",
                f"[CLI connected to {client_address[0]}:{client_address[1]}]",
                "[CLI ==> PRX --- SRV]",
                f"    > {method} {url}",
                f">    {user_agent}",
            ]
        )

        if block_image:
            response = not_found_response("Image blocked by proxy.")
            client_socket.sendall(response)
            log_lines.extend(
                [
                    "[CLI <== PRX --- SRV]",
                    "    > 404 Not Found",
                    ">    text/plain 0bytes",
                ]
            )
            return

        forward_request = build_forward_request(method, target.path, http_version, header_lines, target)

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.connect((target.host, target.port))
        server_socket.sendall(forward_request)

        log_lines.extend(
            [
                f"[SRV connected to {target.host}:{target.port}]",
                "[CLI --- PRX ==> SRV]",
                f"    > {method} {target.host}{target.path}",
                f"    > {user_agent}",
            ]
        )

        response = receive_all(server_socket)
        status, content_type = parse_response_metadata(response)

        log_lines.extend(
            [
                "[CLI --- PRX <== SRV]",
                f"    > {status}",
                f">    {content_type} {len(response)}bytes",
                "[CLI <== PRX --- SRV]",
                f"    > {status}",
                f">    {content_type} {len(response)}bytes",
            ]
        )
        client_socket.sendall(response)

    except OSError as exc:
        log_lines.append(f"[ERROR] {exc}")
    finally:
        if server_socket:
            server_socket.close()
            log_lines.append("[SRV disconnected]")
        client_socket.close()
        log_lines.append("[CLI disconnected]")

        with state.print_lock:
            for line in log_lines:
                print(line)


def run_proxy(config: ProxyConfig) -> None:
    state = SharedState()
    proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_socket.bind(("", config.listen_port))
    proxy_socket.listen(10)

    print(f"Starting proxy server on port {config.listen_port}")
    print(f"Redirect keyword: {config.redirect_keyword!r} -> {config.redirect_host}:{config.redirect_port}")

    try:
        while True:
            client_socket, client_address = proxy_socket.accept()
            worker = threading.Thread(
                target=handle_client,
                args=(client_socket, client_address, config, state),
                daemon=True,
            )
            worker.start()
    except KeyboardInterrupt:
        print("\nProxy server terminated.")
    finally:
        proxy_socket.close()


def parse_args() -> ProxyConfig:
    parser = argparse.ArgumentParser(description="A simple multi-threaded HTTP proxy server.")
    parser.add_argument("listen_port", type=int, help="Local port for the proxy server.")
    parser.add_argument("--redirect-keyword", default=DEFAULT_REDIRECT_KEYWORD)
    parser.add_argument("--redirect-host", default=DEFAULT_REDIRECT_HOST)
    parser.add_argument("--redirect-port", type=int, default=DEFAULT_REDIRECT_PORT)
    args = parser.parse_args()

    return ProxyConfig(
        listen_port=args.listen_port,
        redirect_keyword=args.redirect_keyword.lower(),
        redirect_host=args.redirect_host,
        redirect_port=args.redirect_port,
    )


if __name__ == "__main__":
    run_proxy(parse_args())
