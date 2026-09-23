// Relays the content script's log lines to the Fixr Ping Messager app and its
// phone pings to ntfy (web pages can't make these requests themselves).
const ascii = (s) => String(s || "").replace(/[^\x20-\x7e]/g, "");

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "log" && msg.port) {
    fetch(`http://127.0.0.1:${msg.port}/log`, { method: "POST", body: msg.text })
      .catch(() => {});
  } else if (msg.type === "push" && msg.ntfy) {
    fetch(`${msg.ntfy.server.replace(/\/$/, "")}/${msg.ntfy.topic}`, {
      method: "POST",
      body: msg.message,
      headers: { Title: ascii(msg.title), Priority: "max", Click: msg.url || "" },
    }).catch(() => {});
  }
});
