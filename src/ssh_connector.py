"""
SSH connector for interactive command execution on a remote host.
Uses Paramiko for SSH connectivity with password authentication.
"""

import paramiko


class SSHConnector:
    """SSH connector for remote host using password authentication."""

    def __init__(self, hostname, username, password, port=22, timeout=30):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.client = None

    def connect(self):
        """Establish SSH connection to the remote host."""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.hostname,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=self.timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        return self

    def execute_command(self, command):
        """Execute a non-interactive command and return output."""
        if not self.client:
            raise RuntimeError("Not connected. Call connect() first.")
        stdin, stdout, stderr = self.client.exec_command(command)
        output = stdout.read().decode("utf-8")
        error = stderr.read().decode("utf-8")
        exit_code = stdout.channel.recv_exit_status()
        return {"output": output, "error": error, "exit_code": exit_code}

    def execute_interactive_command(self, command, responses, wait=5, timeout=60):
        """Execute a command that requires interactive input.

        Uses idle-based output detection instead of fixed sleeps: each response
        is sent once the output stream has been silent for 0.3 s, with `wait`
        acting as a safety-cap. This keeps fast commands fast (milliseconds
        instead of `wait` seconds each) while still tolerating slow ones.

        Args:
            command:   The shell command to invoke (e.g. 'bash').
            responses: Ordered list of strings to send to stdin.
            wait:      Max seconds to wait for output after each response.
            timeout:   Max total timeout for the entire session.

        Returns:
            Tuple of (stdout_lines: list[str], stderr: str, exit_code: int)
        """
        import time
        import threading
        import queue as _queue

        if not self.client:
            raise RuntimeError("Not connected. Call connect() first.")

        stdin, stdout, stderr_stream = self.client.exec_command(command, timeout=timeout)

        out_q = _queue.Queue()

        def _reader():
            for line in iter(stdout.readline, ""):
                out_q.put(line)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        collected = []

        def _drain():
            while not out_q.empty():
                line = out_q.get()
                collected.append(line)
                print(line, end="", flush=True)

        def _wait_idle(max_secs, idle_secs=0.3):
            """Return once output has been silent for idle_secs, or max_secs elapsed."""
            deadline = time.time() + max_secs
            last_activity = time.time()
            while time.time() < deadline:
                time.sleep(0.05)
                prev = len(collected)
                _drain()
                if len(collected) > prev:
                    last_activity = time.time()
                elif (time.time() - last_activity) >= idle_secs:
                    break

        _wait_idle(wait)

        for response in responses:
            stdin.write(response + "\n")
            stdin.flush()
            _wait_idle(wait)

        stdin.close()
        reader.join(timeout=timeout)
        _drain()

        stderr_text = stderr_stream.read().decode("utf-8", errors="replace")
        if stderr_text:
            print(stderr_text, end="")
        exit_code = stdout.channel.recv_exit_status()

        return collected, stderr_text, exit_code

    def disconnect(self):
        """Close the SSH connection."""
        if self.client:
            self.client.close()
            self.client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False
