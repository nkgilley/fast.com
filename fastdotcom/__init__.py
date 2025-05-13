import os
import json
import urllib.request
import urllib.parse
import urllib.error
import time
from threading import Thread, Event
import socket
from urllib.parse import urlparse

import asyncio
from icmplib import async_ping, SocketPermissionError
import requests

# Global event to mark when the download has started (i.e. first payload received)
download_started = Event()


# ------------------------------
# Helper Classes and Functions
# ------------------------------

class ProgressReader:
    """
    A file-like object that wraps a bytes payload and tracks its read progress.
    """
    def __init__(self, data, chunk_size=64 * 1024):
        self.data = data
        self.chunk_size = chunk_size
        self.offset = 0
        self.total = len(data)
        self.read_bytes = 0

    def read(self, amt=None):
        if amt is None:
            amt = self.chunk_size
        if self.offset >= self.total:
            return b""
        chunk = self.data[self.offset : self.offset + amt]
        self.offset += amt
        self.read_bytes += len(chunk)
        return chunk


def get_html_result(url, result, index):
    """
    Download helper: Reads data from the URL in fixed-size chunks and updates result[index].
    """
    try:
        req = urllib.request.urlopen(url)
    except urllib.error.URLError:
        result[index] = 0
        return

    chunk_size = 100 * 1024
    i = 1
    while True:
        chunk = req.read(chunk_size)
        if not chunk:
            break
        result[index] = i * chunk_size
        i += 1


def application_bytes_to_network_bits(num_bytes):
    return num_bytes * 8 * 1.0415


def find_ipv4(fqdn):
    ipv4 = socket.getaddrinfo(fqdn, 80, socket.AF_INET)[0][4][0]
    return ipv4


def find_ipv6(fqdn):
    ipv6 = socket.getaddrinfo(fqdn, 80, socket.AF_INET6)[0][4][0]
    return ipv6


def get_fast_token(verbose=False):
    """
    Fetch the fast.com homepage, parse the JavaScript URL, and extract the token.
    Returns the token string or None if there was an error.
    """
    url = "https://fast.com/"
    try:
        url_result = urllib.request.urlopen(url)
    except Exception as e:
        if verbose:
            print("Error connecting to fast.com:", e)
        return None

    response = url_result.read().decode().strip()
    js_name = None
    for line in response.split("\n"):
        if "script src" in line:
            js_name = line.split('"')[1]
            break
    if not js_name:
        if verbose:
            print("Could not find the JavaScript filename.")
        return None

    token_url = "https://fast.com" + js_name
    if verbose:
        print("JavaScript URL is", token_url)
    try:
        url_result = urllib.request.urlopen(token_url)
    except Exception as e:
        if verbose:
            print("Error fetching JavaScript file:", e)
        return None

    js_content = url_result.read().decode().strip()
    token = None
    for part in js_content.split(","):
        if "token:" in part:
            if verbose:
                print("Found JS part:", part)
            token = part.split('"')[1]
            if verbose:
                print("Token is", token)
            break
    if not token and verbose:
        print("Token not found in JavaScript file.")
    return token


# -----------------------------
# Download Test Functions
# -----------------------------

def download_test(
    token,
    verbose=False,
    min_time=5,
    max_time=15,
    force_ipv4=False,
    force_ipv6=False,
    window_size=3,
    connections_min=1,
    connections_max=8,
):
    """
    Multi-threaded download test that dynamically manages parallel connections.
    
    Returns:
        int: Highest moving-average download speed in Mbps (rounded to 0 decimals).
    """
    if not token:
        if verbose:
            print("No token provided. Aborting download.")
        return 0

    base_url = "https://api.fast.com/"
    if force_ipv4:
        ipv4 = find_ipv4("api.fast.com")
        base_url = "http://" + ipv4 + "/"
    elif force_ipv6:
        ipv6 = find_ipv6("api.fast.com")
        base_url = "http://[" + ipv6 + "]/"
    api_url = base_url + "netflix/speedtest?https=true&token=" + token + "&urlCount=3"
    if verbose:
        print("API URL is", api_url)
    try:
        url_result = urllib.request.urlopen(api_url, None, 2)
    except Exception as e:
        if verbose:
            print("Error connecting to API:", e)
        return 0

    json_result = url_result.read().decode().strip()
    parsed_json = json.loads(json_result)
    urls = [item["url"] for item in parsed_json]
    if verbose:
        print("Retrieved URLs:")
        for url in urls:
            print(url)

    results = []
    active_threads = []
    next_url_index = 0

    def spawn_thread():
        nonlocal next_url_index
        idx = len(results)
        results.append(0)
        url = urls[next_url_index]
        next_url_index = (next_url_index + 1) % len(urls)
        t = Thread(target=get_html_result, args=(url, results, idx))
        t.daemon = True
        t.start()
        active_threads.append((t, idx))
        if verbose:
            print(f"Spawned thread {idx} using URL: {url}")

    for _ in range(connections_min):
        spawn_thread()

    snapshots = []
    last_total = 0
    last_snapshot_time = time.perf_counter()
    highest_speed_bps = 0

    start_time = time.perf_counter()
    end_time = start_time + max_time
    current_time = start_time

    while current_time < end_time or (current_time - start_time) < min_time:
        time.sleep(0.5)
        current_time = time.perf_counter()
        active_threads = [(t, idx) for t, idx in active_threads if t.is_alive()]
        while len(active_threads) < connections_min and current_time < end_time:
            spawn_thread()
            active_threads = [(t, idx) for t, idx in active_threads if t.is_alive()]
        if current_time - last_snapshot_time >= 1:
            elapsed_ms = (current_time - last_snapshot_time) * 1000
            total = sum(results)
            delta = total - last_total
            if delta < 0:
                delta = 0
            if delta > 0 and not download_started.is_set():
                download_started.set()
            if elapsed_ms > 0:
                snapshots.append({"bytes": delta, "time": elapsed_ms})
                if len(snapshots) > window_size:
                    snapshots = snapshots[-window_size:]
                total_bytes = sum(s["bytes"] for s in snapshots)
                total_time = sum(s["time"] for s in snapshots)
                avg_speed = (1000 * total_bytes * 8 / total_time) if total_time > 0 else 0
                if avg_speed > highest_speed_bps:
                    highest_speed_bps = avg_speed
                if verbose:
                    print(
                        f"Snapshot: {delta} bytes in {elapsed_ms:.0f} ms, "
                        f"avg speed: {avg_speed:.1f} bps"
                    )
            last_total = total
            last_snapshot_time = current_time

            desired = connections_min
            if avg_speed > 50e6:
                desired = connections_max
            elif avg_speed > 10e6:
                desired = max(connections_min, min(5, connections_max))
            elif avg_speed > 1e6:
                desired = max(connections_min, 3)
            if len(active_threads) < desired and current_time < end_time:
                threads_to_spawn = desired - len(active_threads)
                for _ in range(threads_to_spawn):
                    if len(active_threads) < connections_max:
                        spawn_thread()
                        active_threads = [
                            (t, idx) for t, idx in active_threads if t.is_alive()
                        ]
        if current_time >= end_time and (current_time - start_time) >= min_time:
            break
    time.sleep(0.2)
    mbps = highest_speed_bps / 1e6
    if verbose:
        print(f"Highest moving average download speed: {round(mbps)} Mbps")
    return mbps


# -----------------------------
# Upload Test Functions
# -----------------------------

def upload_with_progress(url, progress_obj, start_time_container, result_container):
    req = urllib.request.Request(url, data=progress_obj, method="POST")
    req.add_header("Content-Length", str(progress_obj.total))
    req.add_header("Content-Type", "application/octet-stream")
    start = time.perf_counter()
    start_time_container[0] = start
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        result_container[0] = (0, time.perf_counter() - start)
        return
    end = time.perf_counter()
    result_container[0] = (progress_obj.read_bytes, end - start)


def upload_test(
    token,
    verbose=False,
    min_time=5,
    max_time=15,
    force_ipv4=False,
    force_ipv6=False,
    window_size=3,
    connections_min=1,
    connections_max=8,
    payload_size=10 * 1024 * 1024,
    max_payload_size=50 * 1024 * 1024,
    adjust_threshold=1.0,
):
    """
    Run an upload test that dynamically manages parallel connections and adjusts the payload size.
    
    Returns:
        int: Highest moving-average upload speed in Mbps (rounded to 0 decimals).
    """
    if not token:
        if verbose:
            print("No token provided. Aborting upload test.")
        return 0

    base_url = "https://api.fast.com/"
    if force_ipv4:
        ipv4 = find_ipv4("api.fast.com")
        base_url = "http://" + ipv4 + "/"
    elif force_ipv6:
        ipv6 = find_ipv6("api.fast.com")
        base_url = "http://[" + ipv6 + "]/"
    api_url = base_url + "netflix/speedtest?https=true&token=" + token + "&urlCount=3"
    if verbose:
        print("API URL is", api_url)
    try:
        url_result = urllib.request.urlopen(api_url, None, 2)
    except Exception as e:
        if verbose:
            print("Error connecting to API:", e)
        return 0

    json_result = url_result.read().decode().strip()
    parsed_json = json.loads(json_result)
    urls = [item["url"] for item in parsed_json]
    if verbose:
        print("Retrieved upload test URLs:")
        for url in urls:
            print(url)

    readers = {}
    start_times = {}
    result_containers = {}
    active_threads = {}
    next_thread_index = 0
    next_url_index = 0
    request_size = payload_size

    def spawn_upload_thread():
        nonlocal next_thread_index, next_url_index, request_size
        idx = next_thread_index
        next_thread_index += 1
        payload = os.urandom(request_size)
        pr = ProgressReader(payload)
        readers[idx] = pr
        start_times[idx] = [None]
        result_containers[idx] = [None]
        url_to_use = urls[next_url_index]
        next_url_index = (next_url_index + 1) % len(urls)
        t = Thread(
            target=upload_with_progress,
            args=(url_to_use, pr, start_times[idx], result_containers[idx]),
        )
        t.daemon = True
        t.start()
        active_threads[idx] = t
        if verbose:
            print(
                f"Spawned upload thread {idx} using URL: {url_to_use} "
                f"with payload size: {request_size} bytes"
            )

    for _ in range(connections_min):
        spawn_upload_thread()

    snapshots = []
    last_total = 0
    last_snapshot_time = time.perf_counter()
    highest_speed_bps = 0

    overall_start_time = time.perf_counter()
    end_time = overall_start_time + max_time
    current_time = overall_start_time

    while current_time < end_time or (current_time - overall_start_time) < min_time:
        time.sleep(1)
        current_time = time.perf_counter()
        total = 0
        finished_times = []
        for idx in list(active_threads.keys()):
            t = active_threads[idx]
            if t.is_alive():
                total += readers[idx].read_bytes
            else:
                if result_containers[idx][0] is not None:
                    bytes_uploaded, elapsed = result_containers[idx][0]
                    total += bytes_uploaded
                    finished_times.append(elapsed)
                active_threads.pop(idx)
        elapsed_ms = (current_time - last_snapshot_time) * 1000
        delta = total - last_total
        if delta < 0:
            delta = 0
        if elapsed_ms > 0:
            snapshots.append({"bytes": delta, "time": elapsed_ms})
            if len(snapshots) > window_size:
                snapshots = snapshots[-window_size:]
            total_bytes = sum(s["bytes"] for s in snapshots)
            total_time = sum(s["time"] for s in snapshots)
            avg_speed = (1000 * total_bytes * 8 / total_time) if total_time > 0 else 0
            if avg_speed > highest_speed_bps:
                highest_speed_bps = avg_speed
            if verbose:
                print(
                    f"Snapshot: {delta} bytes in {elapsed_ms:.0f} ms, "
                    f"avg speed: {avg_speed:.1f} bps"
                )
            if finished_times:
                min_elapsed = min(finished_times)
                if min_elapsed < adjust_threshold:
                    new_size = int(request_size * (adjust_threshold / min_elapsed))
                    if new_size > request_size:
                        old_size = request_size
                        request_size = min(new_size, max_payload_size)
                        if verbose and request_size != old_size:
                            print(f"Adjusted payload size to: {request_size} bytes")
        last_total = total
        last_snapshot_time = current_time

        if avg_speed > 50e6:
            desired = connections_max
        elif avg_speed > 10e6:
            desired = max(connections_min, 5)
        elif avg_speed > 1e6:
            desired = max(connections_min, 3)
        else:
            desired = connections_min
        if len(active_threads) < desired and current_time < end_time:
            threads_to_spawn = desired - len(active_threads)
            for _ in range(threads_to_spawn):
                if len(active_threads) < connections_max:
                    spawn_upload_thread()
        if current_time >= end_time and (current_time - overall_start_time) >= min_time:
            break
    time.sleep(0.2)
    mbps = highest_speed_bps / 1e6
    if verbose:
        print(f"Highest moving average upload speed: {round(mbps)} Mbps")
    return mbps


def upload_js_comparable_test(token, verbose=False, max_time=15, payload_chunk_size=64 * 1024):
    """
    An upload test that mimics JavaScript's behavior by using a persistent HTTP connection.
    It streams random data (via a generator) for 'max_time' seconds using chunked transfer encoding.
    
    Returns:
        int: The upload speed in Mbps (rounded to 0 decimals).
    """
    if not token:
        if verbose:
            print("No token provided. Aborting JS-comparable upload test.")
        return 0

    base_url = "https://api.fast.com/"
    api_url = base_url + "netflix/speedtest?https=true&token=" + token + "&urlCount=3"
    try:
        url_result = urllib.request.urlopen(api_url, None, 2)
    except Exception as e:
        if verbose:
            print("Error connecting to API for upload:", e)
        return 0

    json_result = url_result.read().decode().strip()
    parsed_json = json.loads(json_result)
    urls = [item["url"] for item in parsed_json]
    url_to_use = urls[0]
    if verbose:
        print("Using upload URL (JS-comparable):", url_to_use)

    start = time.perf_counter()
    total_bytes = 0

    def data_generator():
        nonlocal total_bytes
        while time.perf_counter() - start < max_time:
            chunk = os.urandom(payload_chunk_size)
            total_bytes += len(chunk)
            yield chunk

    session = requests.Session()
    headers = {
        "Transfer-Encoding": "chunked",
        "Content-Type": "application/octet-stream",
    }
    try:
        session.post(url_to_use, data=data_generator(), headers=headers, timeout=max_time + 5)
    except Exception as e:
        if verbose:
            print("JS-comparable upload error:", e)
        return 0
    elapsed = time.perf_counter() - start
    speed_bps = (total_bytes * 8) / elapsed
    mbps = speed_bps / 1e6
    if verbose:
        print(f"Uploaded {total_bytes} bytes in {round(elapsed)} s, speed: {round(mbps)} Mbps")
    return mbps


def upload_js_comparable_dynamic_test(token, verbose=False, max_time=15, candidate_sizes=None):
    """
    Runs a JS-comparable upload test that cycles through different payload chunk sizes
    repeatedly until the overall max_time is reached, then returns the highest measured upload speed.
    
    Parameters:
        candidate_sizes: list of payload chunk sizes in bytes.
                         Defaults to [64KB, 128KB, 256KB, 512KB].
    
    Returns:
        int: Highest upload speed in Mbps (rounded to 0 decimals).
    """
    if candidate_sizes is None:
        candidate_sizes = [64 * 10240, 128 * 1024, 256 * 1024, 512 * 1024]

    best_speed = 0
    start_time = time.perf_counter()
    # Cycle through candidate sizes until max_time is reached.
    while time.perf_counter() - start_time < max_time:
        for size in candidate_sizes:
            remaining = max_time - (time.perf_counter() - start_time)
            if remaining <= 0:
                break
            speed = upload_js_comparable_test(token, verbose=verbose, max_time=remaining, payload_chunk_size=size)
            if verbose:
                print(f"Payload size: {size} bytes -> Upload speed: {speed} Mbps")
            if speed > best_speed:
                best_speed = speed
    if verbose:
        print(f"Best upload speed measured: {best_speed} Mbps")
    return best_speed


# -----------------------------
# Ping Test Functions Using icmplib
# -----------------------------

def ping_test(host, count=5, timeout=2, verbose=False):
    """
    Uses icmplib's async_ping to send ICMP echo requests to 'host' count times.
    Returns the average latency (in ms) and a list of individual measurements.
    (Values are already in ms.)
    If an error occurs, returns 0 and an empty list.
    """
    try:
        response = asyncio.run(async_ping(host, count=count, timeout=timeout, privileged=True))
    except SocketPermissionError:
        if verbose:
            print("Insufficient privileges, falling back to unprivileged mode for ping")
        try:
            response = asyncio.run(
                async_ping(host, count=count, timeout=timeout, privileged=False)
            )
        except Exception as e:
            if verbose:
                print("Ping error (unprivileged):", e)
            return 0, []
    except Exception as e:
        if verbose:
            print("Ping error:", e)
        return 0, []
    avg_ms = response.avg_rtt
    times_ms = response.rtts
    if verbose:
        print(f"ICMP ping to {host}: avg {round(avg_ms)} ms, individual: {[round(r) for r in times_ms]}")
    return avg_ms, times_ms


def fast_com_ping_unloaded(token=None, verbose=False, count=5):
    """
    Measures unloaded ping using icmplib by extracting the hostname from a test URL.
    
    Returns:
        (average latency in ms, list of measurements)
    If token retrieval fails, returns 0 for the average ping.
    """
    if token is None:
        token = get_fast_token(verbose=verbose)
    if not token:
        return 0, []
    base_url = "https://api.fast.com/"
    api_url = base_url + "netflix/speedtest?https=true&token=" + token + "&urlCount=3"
    try:
        url_result = urllib.request.urlopen(api_url, None, 2)
    except Exception as e:
        if verbose:
            print("Error connecting to API for unloaded ping:", e)
        return 0, []
    json_result = url_result.read().decode().strip()
    parsed_json = json.loads(json_result)
    urls = [item["url"] for item in parsed_json]
    host = urlparse(urls[0]).hostname
    avg, times = ping_test(host, count=count, timeout=2, verbose=verbose)
    return avg, times


def fast_com_download(token=None, verbose=False, max_time=15, ping_count=5, ping_interval=1):
    """
    Runs the multi-threaded download test and concurrently measures loaded ping using icmplib.
    The ping worker starts after the first payload is received and runs until the download test completes.
    
    Returns:
        (download speed in Mbps, average loaded ping in ms, list of ping measurements)
    If token retrieval fails, returns 0 for both download speed and ping.
    """
    if token is None:
        token = get_fast_token(verbose=verbose)
    if not token:
        return 0, 0, []

    base_url = "https://api.fast.com/"
    api_url = base_url + "netflix/speedtest?https=true&token=" + token + "&urlCount=3"
    try:
        url_result = urllib.request.urlopen(api_url, None, 2)
    except Exception as e:
        if verbose:
            print("Error connecting to API for download:", e)
        return 0, 0, []
    json_result = url_result.read().decode().strip()
    parsed_json = json.loads(json_result)
    urls = [item["url"] for item in parsed_json]
    host = urlparse(urls[0]).hostname
    if verbose:
        print("Using host for loaded ping (ICMP):", host)

    ping_values = []
    stop_event = Event()

    def ping_worker():
        download_started.wait()
        while not stop_event.is_set():
            avg, _ = ping_test(host, count=ping_count, timeout=ping_interval, verbose=verbose)
            if avg is not None:
                ping_values.append(avg)
            time.sleep(ping_interval)

    ping_thread = Thread(target=ping_worker)
    ping_thread.daemon = True
    ping_thread.start()

    dl_speed = download_test(
        token,
        verbose=verbose,
        min_time=5,
        max_time=max_time,
        force_ipv4=False,
        force_ipv6=False,
        window_size=3,
        connections_min=1,
        connections_max=8,
    )

    stop_event.set()
    ping_thread.join()
    overall_ping = sum(ping_values) / len(ping_values) if ping_values else 0
    return dl_speed, overall_ping, ping_values


# -----------------------------
# Convenience Functions
# -----------------------------

def fast_com_test():
    result = {
    "download_speed": 1.23,
    "upload_speed": 4.56,
    "unloaded_ping": 7.89,
    "loaded_ping": 10.11,
    }
    return result

def fast_com(
    verbose=False,
    max_time=15,
    ping_count=5,
    force_ipv4=False,
    force_ipv6=False,
    payload_size=10 * 1024 * 1024,
    max_payload_size=50 * 1024 * 1024,
):
    token = get_fast_token(verbose=verbose)
    if not token:
        return {
        "download_speed": 0,
        "upload_speed": 0,
        "unloaded_ping": 0,
        "loaded_ping": 0,
        "success": 0,
        }

    avg_ping_unloaded, ping_unloaded_vals = fast_com_ping_unloaded(
        token=token, verbose=verbose, count=ping_count
    )
    dl_speed_loaded, avg_ping_loaded, ping_loaded_vals = fast_com_download(
        token=token, verbose=verbose, max_time=max_time, ping_count=ping_count, ping_interval=1
    )
    upload_speed_dynamic = upload_js_comparable_dynamic_test(
        token=token, verbose=verbose, max_time=max_time
    )

    result = {
    "download_speed": dl_speed_loaded,
    "upload_speed": upload_speed_dynamic,
    "unloaded_ping": avg_ping_unloaded,
    "loaded_ping": avg_ping_loaded,
    "success": 1,
    }
    return result


def main():
    print("Starting fast.com speed test...")

    token = get_fast_token(verbose=False)
    if not token:
        print("Failed to get token. All test values will be 0.")
        return

    print("\nRunning unloaded ping test:")
    avg_ping_unloaded, ping_unloaded_vals = fast_com_ping_unloaded(
        token=token, verbose=False, count=3
    )
    print("Unloaded Ping: {:d} ms".format(int(round(avg_ping_unloaded))))

    print("\nRunning download test:")
    dl_speed_loaded, avg_ping_loaded, ping_loaded_vals = fast_com_download(
        token=token, verbose=False, max_time=10, ping_count=5, ping_interval=1
    )
    print("Download test results: {:.2f} Mbps".format(dl_speed_loaded))
    print("Loaded Ping: {:d} ms".format(int(round(avg_ping_loaded))))

    print("\nRunning upload test:")
    upload_speed_dynamic = upload_js_comparable_dynamic_test(
        token=token, verbose=False, max_time=10
    )
    print("Upload test result: {:.2f} Mbps".format(upload_speed_dynamic))

    print("All tests completed.")


if __name__ == "__main__":
    main()
