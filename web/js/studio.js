/* Studio: the Make tab's home. Three makers that build videos from scratch
 * (money explainer, trending reels, a reel from a pasted post) plus a door
 * to the classic clip-a-long-video flow. Runs are started through
 * /api/studio/run and watched by polling /api/studio/status. */
import { api } from "./api.js";
import { el, toast } from "./ui.js";

const POLL_MS = 4000;

export function mountStudio(view, { openWorkspace }) {
  let timer = null;
  let wasRunning = false;

  const statusTitle = el("div", { class: "studio-status-title" }, "Nothing is running");
  const statusMeta = el("div", { class: "studio-status-meta" }, "");
  const logBox = el("pre", { class: "studio-log", "aria-live": "polite" });
  const statusCard = el("section", { class: "studio-status" },
    el("div", { class: "studio-status-head" },
      el("span", { class: "studio-dot" }), statusTitle, el("span", { class: "spacer" }), statusMeta),
    logBox);

  const buttons = [];
  async function start(body, btn) {
    btn.disabled = true;
    try {
      await api.post("/api/studio/run", body);
      toast("Started. You can leave this page; it keeps running.", "ok");
      await refresh();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  function card({ tag, title, text, extra, action, label, links = [] }) {
    const btn = el("button", { class: "btn btn-primary", type: "button" }, label);
    btn.addEventListener("click", () => action(btn));
    buttons.push(btn);
    return el("article", { class: "studio-card" },
      el("span", { class: "studio-tag" }, tag),
      el("h2", {}, title),
      el("p", {}, text),
      extra || null,
      el("div", { class: "studio-actions" }, btn,
        ...links.map(([text2, fn]) => {
          const a = el("button", { class: "btn btn-ghost", type: "button" }, text2);
          a.addEventListener("click", fn);
          return a;
        })));
  }

  // --- money video
  const topic = el("input", { class: "input", type: "text", maxlength: "200",
    placeholder: "Topic (optional)",
    "aria-label": "Money video topic" });
  const money = card({
    tag: "Money channel", title: "Money video + reels",
    text: "One 4-5 minute explainer and its vertical reels, fact-checked and scored. About 30 minutes.",
    extra: topic, label: "Make it",
    action: (btn) => start({ kind: "money", topic: topic.value }, btn),
    links: [["See results", () => openWorkspace("money", "Money")]],
  });

  // --- trending reels
  const count = el("select", { class: "input", "aria-label": "How many reels" },
    ...[1, 2, 3].map((n) => el("option", { value: String(n), ...(n === 2 ? { selected: "" } : {}) },
      `${n} reel${n > 1 ? "s" : ""}`)));
  const trending = card({
    tag: "Trending channel", title: "Today's funny reels",
    text: "Finds today's most talked-about tech and business stories, writes the funniest, and drops any script that scores under 70.",
    extra: count, label: "Find and make",
    action: (btn) => start({ kind: "trending", count: Number(count.value) }, btn),
    links: [["See results", () => openWorkspace("trending", "Trending")]],
  });

  // --- reel from a post
  const text = el("textarea", { class: "input", rows: "3", maxlength: "1200",
    placeholder: "Paste the post or headline", "aria-label": "Post text" });
  const author = el("input", { class: "input", type: "text", maxlength: "80",
    placeholder: "Who posted it", "aria-label": "Who posted it" });
  const context = el("textarea", { class: "input", rows: "2", maxlength: "4000",
    placeholder: "Background facts it may use (optional, but makes it better)",
    "aria-label": "Background facts" });
  const story = card({
    tag: "Trending channel", title: "Reel from a post",
    text: "Saw something viral? Paste it and get a narrated reel. It only uses the facts you give it.",
    extra: el("div", { class: "studio-fields" }, text, author, context), label: "Make the reel",
    action: (btn) => start({ kind: "story", text: text.value, author: author.value,
      context: context.value }, btn),
  });

  // --- classic clipping
  const clip = el("article", { class: "studio-card studio-card-quiet" },
    el("span", { class: "studio-tag" }, "Classic"),
    el("h2", {}, "Clip a long video"),
    el("p", {}, "Turn a long video or a link into short vertical clips with captions."),
    el("div", { class: "studio-actions" },
      el("a", { class: "btn", href: "#/clip" }, "Open clipping")));

  view.append(
    el("div", { class: "studio" },
      el("header", { class: "studio-head" },
        el("h1", {}, "What are we making?"),
        el("p", {}, "Everything waits for your approval. Nothing posts on its own.")),
      statusCard,
      el("div", { class: "studio-grid" }, money, trending, story, clip)));

  async function refresh() {
    let s;
    try { s = await api.get("/api/studio/status"); } catch { return; }
    statusCard.classList.toggle("is-running", s.running);
    if (s.running) {
      statusTitle.textContent = `Making: ${s.label}`;
      statusMeta.textContent = `${s.minutes} min`;
    } else if (s.exit_code === null) {
      statusTitle.textContent = "Nothing is running";
      statusMeta.textContent = "";
    } else {
      statusTitle.textContent = s.exit_code === 0 ? `Finished: ${s.label}`
        : `${s.label} stopped with a problem`;
      statusMeta.textContent = s.exit_code === 0 ? "Open See results to approve it" : "";
    }
    logBox.textContent = s.lines.join("\n");
    logBox.hidden = s.lines.length === 0;
    logBox.scrollTop = logBox.scrollHeight;
    buttons.forEach((b) => { b.disabled = s.running; });
    if (wasRunning && !s.running) {
      toast(s.exit_code === 0 ? "Done. It is waiting in approvals." : "That run hit a problem.",
        s.exit_code === 0 ? "ok" : "error");
    }
    wasRunning = s.running;
  }

  refresh();
  timer = setInterval(refresh, POLL_MS);
  return () => clearInterval(timer);
}
