/* Thin client for the ClipForge API. Every helper throws an Error whose
 * message is the server's plain-language sentence, ready to show verbatim. */

async function request(method, url, body) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error("Can't reach ClipForge — is the app still running?");
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    throw new Error((data && data.detail) || "Something went wrong — try again.");
  }
  return data;
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body),
  put: (url, body) => request("PUT", url, body),
};

/* Multipart upload with progress (fetch has no upload progress; XHR does). */
export function uploadFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* keep null */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error((data && data.detail)
        || "Uploading that file didn't work — try again."));
    };
    xhr.onerror = () =>
      reject(new Error("Can't reach ClipForge — is the app still running?"));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

/* Live run progress: WebSocket first, 2s polling fallback if the socket
 * drops while the run is still going. Returns a stop() function. */
export function watchRun(runId, handlers) {
  let finished = false;
  let poll = 0;
  let ws = null;

  const terminal = (type, payload) => {
    if (finished) return;
    finished = true;
    stop();
    if (type === "done") handlers.onDone(payload.result);
    else if (type === "cancelled") handlers.onCancelled();
    else handlers.onError(payload.error || "This run didn't finish — try again.");
  };

  const startPolling = () => {
    if (finished || poll) return;
    poll = setInterval(async () => {
      try {
        const st = await api.get(`/api/runs/${runId}`);
        if (st.snapshot) handlers.onSnapshot(st.snapshot);
        if (st.state !== "running") terminal(st.state, st);
      } catch (e) {
        terminal("error", { error: e.message });
      }
    }, 2000);
  };

  const proto = location.protocol === "https:" ? "wss" : "ws";
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws/runs/${runId}`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") handlers.onSnapshot(msg.data);
      else terminal(msg.type, msg);
    };
    ws.onerror = startPolling;
    ws.onclose = () => { if (!finished) startPolling(); };
  } catch {
    startPolling();
  }

  function stop() {
    if (poll) { clearInterval(poll); poll = 0; }
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
  }
  return stop;
}
