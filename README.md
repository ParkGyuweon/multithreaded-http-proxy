# Multi-threaded HTTP Proxy Server

A lightweight HTTP proxy server implemented with Python sockets and threads.

The project focuses on the core mechanics of proxy behavior: accepting multiple clients concurrently, parsing HTTP requests, forwarding traffic to origin servers, rewriting connection headers, optionally redirecting selected requests, and filtering image resources.

## Features

- Handles multiple client connections with one worker thread per request
- Parses absolute-form HTTP request targets and `Host` headers
- Rewrites requests for origin-form forwarding
- Forces `Connection: close` for simpler response boundary handling
- Redirects matching requests to a configurable target host
- Supports runtime image filtering toggled by URL keywords
- Prints synchronized request/response logs across worker threads

## Limitations

- Supports plain HTTP traffic only
- Does not implement HTTPS `CONNECT` tunneling
- Uses a simple thread-per-connection model intended for learning and small-scale testing

## Requirements

- Python 3.10+
- No external packages

## Usage

Start the proxy:

```bash
python proxy_server.py 8080
```

Run with custom redirection settings:

```bash
python proxy_server.py 8080 --redirect-keyword google --redirect-host mnet.yonsei.ac.kr --redirect-port 80
```

Configure a browser or HTTP client to use `localhost:8080` as an HTTP proxy.

Example with `curl`:

```bash
curl -x http://localhost:8080 http://example.com/
```

Turn image filtering on or off by requesting a URL that contains one of these keywords:

```text
image_off  -> enable image blocking
image_on   -> disable image blocking
```

When filtering is enabled, requests for common image extensions such as `.png`, `.jpg`, `.gif`, `.svg`, and `.webp` receive a `404 Not Found` response from the proxy.

## Project Structure

```text
.
├── proxy_server.py
└── README.md
```

## What I Practiced

- TCP socket programming
- HTTP request parsing and forwarding
- Multi-threaded server design
- Shared-state synchronization with locks
- Defensive handling of malformed requests
- Simple traffic control through redirection and filtering
