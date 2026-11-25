"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="agent-token"]').content;
const efforts = { none: "关闭", low: "低", medium: "中", high: "高", xhigh: "超高", max: "最高" };
const toolLabels = {
  list_files: "列出文件", read_file: "读取文件", search_project: "搜索项目", git_status: "查看 Git 状态",
  edit_file: "修改文件", create_file: "创建文件", run_command: "运行命令",
};
let state = null;
let task = null;
let live = null;
let pending = null;
let loading = false;
let polling = false;
let toastTimer;

function make(tag, name, value) {
  const element = document.createElement(tag);
  if (name) element.className = name;
  if (value !== undefined) element.textContent = value;
  return element;
}

function button(label, onClick, primary = false) {
  const element = make("button", primary ? "button primary" : "button", label);
  element.type = "button";
  element.addEventListener("click", onClick);
  return element;
}

async function api(path, body) {
  const headers = { Authorization: "Bearer " + token };
  const options = { headers, cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/api/" + path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失败。");
  return result;
}

function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 5500);
}

function watch(promise) {
  return promise.catch((error) => toast(error.message));
}

function openDialog(id) {
  if (!$(id).open) $(id).showModal();
}

function openProject() {
  $("project-error").textContent = "";
  if (task && !task.demo) $("project-path").value = task.root;
  openDialog("project-dialog");
}

function renderTasks() {
  const fragment = document.createDocumentFragment();
  for (const item of state.tasks) {
    const entry = make("button", "task-button");
    entry.type = "button";
    entry.setAttribute("aria-current", String(task?.id === item.id));
    entry.title = item.root;
    const labels = make("span", "task-text");
    labels.append(make("span", "", item.title), make("small", "", item.demo ? "离线演示" : item.root.split(/[\\/]/).pop()));
    entry.append(make("span", "muted", state.active === item.id ? "●" : "▱"), labels);
    entry.addEventListener("click", () => {
      if (task?.id !== item.id) watch(openTask(item.id));
    });
    fragment.append(entry);
  }
  $("tasks").replaceChildren(fragment);
  const count = state.plugins.filter((plugin) => plugin.state === "connected").length;
  $("plugin-count").textContent = count ? String(count) : "";
}

function renderModels(model, effort) {
  const select = $("model");
  const options = document.createDocumentFragment();
  for (const [id, provider] of Object.entries(state.providers)) {
    const group = document.createElement("optgroup");
    group.label = provider.name;
    for (const [modelId, spec] of Object.entries(state.models)) {
      if (spec.provider !== id) continue;
      const option = new Option(spec.name + (provider.status === "missing" ? " · 缺少 Key" : ""), modelId);
      group.append(option);
    }
    options.append(group);
  }
  if (task?.demo) options.append(new Option("本地演示", "local-demo"));
  select.replaceChildren(options);
  select.value = model;
  if (!select.value) select.value = "gpt-6.2-sol";
  renderEffort(effort);
}

function renderEffort(selected) {
  const spec = state.models[$("model").value];
  const select = $("effort");
  select.replaceChildren(...spec.efforts.map((name) => new Option(efforts[name], name)));
  select.value = spec.efforts.includes(selected) ? selected : spec.default;
  renderComposer();
}

function appendInline(root, text) {
  const tokens = /\x60([^\x60\n]+)\x60|\*\*([^*\n]+)\*\*/g;
  let cursor = 0;
  for (const match of text.matchAll(tokens)) {
    root.append(document.createTextNode(text.slice(cursor, match.index)));
    root.append(make(match[1] ? "code" : "strong", "", match[1] || match[2]));
    cursor = match.index + match[0].length;
  }
  root.append(document.createTextNode(text.slice(cursor)));
}

function markdown(root, text) {
  const fragment = document.createDocumentFragment();
  let paragraph = [];
  let list = null;
  let code = null;
  const flush = () => {
    if (!paragraph.length) return;
    const p = make("p");
    appendInline(p, paragraph.join("\n"));
    fragment.append(p);
    paragraph = [];
  };
  for (const line of text.split(/\r?\n/)) {
    if (/^\x60\x60\x60/.test(line)) {
      flush();
      list = null;
      if (code) {
        fragment.append(make("pre", "", code.join("\n")));
        code = null;
      } else {
        code = [];
      }
    } else if (code) {
      code.push(line);
    } else if (/^#{1,6} /.test(line)) {
      flush();
      list = null;
      const h = make("h3");
      appendInline(h, line.replace(/^#+ /, ""));
      fragment.append(h);
    } else if (/^(?:[-*]|\d+\.) /.test(line)) {
      flush();
      const type = /^\d/.test(line) ? "ol" : "ul";
      if (!list || list.localName !== type) {
        list = make(type);
        fragment.append(list);
      }
      const li = make("li");
      appendInline(li, line.replace(/^(?:[-*]|\d+\.) /, ""));
      list.append(li);
    } else if (line.trim()) {
      list = null;
      paragraph.push(line);
    } else {
      flush();
      list = null;
    }
  }
  if (code) fragment.append(make("pre", "", code.join("\n")));
  flush();
  root.replaceChildren(fragment);
}

function modelLabel(model, effort) {
  return [state.models[model]?.name || model, effort !== "none" ? efforts[effort] : ""].filter(Boolean).join(" · ");
}

function messageRow(data, isLive = false) {
  const item = make("article", "message " + data.role);
  if (data.role === "assistant") item.append(make("div", "message-label", modelLabel(data.model, data.effort)));
  const body = make("div", "message-text");
  if (data.role === "user") body.textContent = data.text;
  else markdown(body, data.text);
  if (isLive) body.id = "live-text";
  item.append(body);
  if (data.info) item.append(make("p", data.status === "error" ? "error" : "muted", data.info));
  return item;
}

function scrollChat(always = false) {
  const view = $("conversation");
  if (always || view.scrollHeight - view.scrollTop - view.clientHeight < 160) view.scrollTop = view.scrollHeight;
}

function renderMessages() {
  $("welcome").hidden = Boolean(task);
  $("messages").hidden = !task;
  $("messages").replaceChildren();
  if (!task) return;
  if (!task.messages.length) {
    $("messages").append(make("p", "empty", "可以让 Agent 先检查项目，也可以直接描述想改的功能。"));
  }
  for (const message of task.messages) $("messages").append(messageRow(message));
  if (live) $("messages").append(messageRow({ ...live, role: "assistant", text: live.text }, true));
  scrollChat(true);
}

function renderComposer() {
  $("composer").hidden = !task;
  if (!task) return;
  const provider = state.models[$("model").value]?.provider;
  const configured = provider === "demo" || state.providers[provider]?.status !== "missing";
  const runningHere = state.active === task.id || Boolean(live);
  $("send").disabled = loading || Boolean(state.active) || !configured;
  $("stop").hidden = !runningHere;
  $("stop").disabled = !live?.id || live.stopping;
  $("model").disabled = runningHere;
  $("effort").disabled = runningHere;
  const status = $("composer-status");
  if (runningHere) status.textContent = pending ? "等待你审批" : live?.status || "正在启动…";
  else if (state.active) status.textContent = "另一个任务正在运行";
  else if (!configured) status.textContent = "请在「模型与 API Key」中配置该供应商";
  else status.textContent = "Ctrl + Enter 发送";
}

function addActivity(data, target = $("activity-pane")) {
  target.querySelector(".empty")?.remove();
  const item = make("details", "activity-item");
  item.append(make("summary", "", (toolLabels[data.name] || data.name) + (data.path ? " · " + data.path : "")));
  item.append(make("pre", "", data.output));
  target.prepend(item);
}

function renderInspector() {
  const activity = $("activity-pane");
  activity.replaceChildren(make("p", "empty", task ? "工具活动会显示在这里。" : "打开任务后查看工具活动。"));
  for (const message of task?.messages || []) {
    for (const event of message.activity || []) addActivity(event, activity);
  }
  for (const event of live?.activity || []) addActivity(event, activity);
  const changes = $("changes-pane");
  changes.replaceChildren();
  $("change-count").textContent = task?.changes.length || "";
  if (!task?.changes.length) changes.append(make("p", "empty", "已应用的文件改动会显示在这里。"));
  const descriptions = { applied: "已应用", undone: "已撤销", conflict: "发生冲突", applying: "正在写入", undoing: "正在撤销" };
  for (const change of [...(task?.changes || [])].reverse()) {
    const item = make("div", "change-item");
    item.append(make("strong", "", change.path), make("div", "muted", descriptions[change.status] || change.status));
    const actions = make("div", "approval-actions");
    actions.append(button("查看差异", () => showDiff(change.path, change.diff)));
    if (change.status === "applied") {
      actions.append(button("撤销", () => {
        const taskId = task.id;
        if (window.confirm("撤销工作台对这个文件的这次改动？")) {
          watch(api("undo", { task: taskId, change: change.id }).then((updated) => {
            if (task?.id === taskId) {
              task = updated;
              renderInspector();
            }
            toast("文件改动已撤销");
          }));
        }
      }));
    }
    item.append(actions);
    changes.append(item);
  }
}

function showDiff(title, diff, approval = null) {
  $("review-title").textContent = title;
  const root = $("review-body");
  const preview = make("pre", "review-pre");
  for (const line of diff.split("\n")) {
    let style = "diff-line";
    if (line.startsWith("@@") || line.startsWith("+++") || line.startsWith("---")) style += " diff-meta";
    else if (line.startsWith("+")) style += " diff-add";
    else if (line.startsWith("-")) style += " diff-remove";
    preview.append(make("span", style, line));
  }
  root.replaceChildren(preview);
  if (approval) {
    const actions = make("div", "dialog-actions");
    actions.append(button("拒绝", () => watch(decide(approval, false))));
    actions.append(button("批准应用", () => watch(decide(approval, true)), true));
    root.append(actions);
  }
  openDialog("review-dialog");
}

async function decide(approval, allow) {
  await api("decide", { run: live.id, id: approval.id, allow });
  setApproval(null);
  if ($("review-dialog").open) $("review-dialog").close();
}

function setApproval(approval) {
  if (pending?.id === approval?.id) return;
  pending = approval;
  const root = $("approval");
  root.hidden = !approval;
  root.replaceChildren();
  if (!approval) return;
  root.append(make("strong", "", approval.title));
  const summary = approval.kind === "edit" ? approval.path : approval.kind === "command"
    ? approval.cwd + "\n" + approval.command
    : approval.plugin + " / " + approval.tool + "\n" + approval.args;
  const contents = make(approval.kind === "edit" ? "div" : "details", "approval-summary");
  if (approval.kind === "edit") {
    contents.textContent = summary;
  } else {
    contents.open = true;
    contents.append(make("summary", "", "查看参数"), make("pre", "review-pre", summary));
  }
  root.append(contents);
  const actions = make("div", "approval-actions");
  actions.append(button("拒绝", () => watch(decide(approval, false))));
  if (approval.kind === "edit") {
    actions.append(button("查看差异", () => showDiff(approval.path, approval.diff, approval), true));
  } else {
    actions.append(button(approval.kind === "command" ? "批准运行" : "批准调用",
      () => watch(decide(approval, true)), true));
  }
  root.append(actions);
}

function setTask(next) {
  task = next;
  live = null;
  setApproval(null);
  if (task) localStorage.setItem("active-task", task.id);
  $("task-title").textContent = task?.title || "工作台";
  $("task-path").textContent = task?.root || "";
  $("task-path").title = task?.root || "";
  $("prompt").value = "";
  if (task) renderModels(task.model, task.effort);
  renderTasks();
  renderMessages();
  renderInspector();
  renderComposer();
}

async function openTask(id) {
  const next = await api("task?id=" + encodeURIComponent(id));
  setTask(next);
  await poll();
}

async function startPrompt() {
  if (!task || loading || state.active) return;
  const prompt = $("prompt").value.trim();
  if (!prompt) {
    $("prompt").focus();
    return;
  }
  const model = $("model").value;
  const effort = $("effort").value;
  const provider = state.models[model].provider;
  const previous = [...task.messages].reverse().find((item) => item.model);
  let share = false;
  if (previous && provider !== "demo" && state.models[previous.model]?.provider !== provider) {
    share = window.confirm("将近期任务对话发送给 " + state.providers[provider].name + " 以继续当前任务？");
    if (!share) return;
  }
  const id = task.id;
  loading = true;
  renderComposer();
  try {
    await api("run", { task: id, prompt, model, effort, share_context: share });
    if (task?.id === id) {
      task = await api("task?id=" + id);
      $("prompt").value = "";
      $("task-title").textContent = task.title;
      renderMessages();
    }
    state.active = id;
    await poll();
  } finally {
    loading = false;
    renderComposer();
  }
}

async function poll() {
  if (polling || !task || state.active !== task.id && !live) return;
  polling = true;
  const id = task.id;
  try {
    const snapshot = await api("run?task=" + id + "&after=" + (live?.count || 0) + "&run=" + (live?.id || ""));
    if (!snapshot || task?.id !== id) return;
    if (!live || live.id !== snapshot.id) {
      live = { id: snapshot.id, model: snapshot.model, effort: snapshot.effort, text: "", activity: [] };
      renderMessages();
    }
    live.count = snapshot.count;
    live.stopping = snapshot.stopping;
    let updated = false;
    for (const event of snapshot.events) {
      if (event.type === "text") {
        live.text += event.text;
        updated = true;
      } else if (event.type === "result") {
        live.activity.push(event);
        addActivity(event);
      } else if (event.type === "status") {
        live.status = event.text;
      } else if (event.type === "tool") {
        live.status = toolLabels[event.name] || event.name;
      }
    }
    if (updated && $("live-text")) {
      const view = $("conversation");
      const follow = view.scrollHeight - view.scrollTop - view.clientHeight < 110;
      markdown($("live-text"), live.text);
      if (follow) scrollChat(true);
    }
    setApproval(snapshot.approval);
    if (snapshot.done) {
      const finished = await api("task?id=" + id);
      const nextState = await api("state");
      if (task?.id === id) {
        task = finished;
        live = null;
        setApproval(null);
        $("task-title").textContent = task.title;
        renderMessages();
        renderInspector();
      }
      state = nextState;
      renderTasks();
    }
    renderComposer();
  } finally {
    polling = false;
  }
}

async function refresh() {
  const activeBefore = state?.active;
  state = await api("state");
  if (live || activeBefore !== state.active) renderComposer();
  renderTasks();
  if ($("plugins-dialog").open) renderPlugins();
  await poll();
}

function keyRows() {
  const root = $("key-rows");
  root.replaceChildren();
  for (const [id, provider] of Object.entries(state.providers)) {
    const row = make("section", "key-row");
    const heading = make("div", "key-title");
    const label = make("span", "muted");
    const link = make("a", "", "获取 API Key ↗");
    link.href = provider.key_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    heading.append(make("strong", "", provider.name), label, link);
    const entry = make("form", "key-inputs");
    const input = make("input");
    input.type = "password";
    input.setAttribute("aria-label", provider.name + " API Key");
    input.placeholder = "输入 API Key";
    input.autocomplete = "new-password";
    input.spellcheck = false;
    input.maxLength = 800;
    const save = make("button", "button primary", "保存");
    save.type = "submit";
    const failure = make("div", "error");
    failure.setAttribute("role", "status");
    const remove = button("移除", () => {
      if (window.confirm("移除本机保存的 " + provider.name + " API Key？")) {
        watch(api("key", { provider: id, remove: true }).then(({ status }) => setStatus(status)));
      }
    });
    const testing = make("div", "test-key");
    const select = make("select");
    select.setAttribute("aria-label", provider.name + " 测试模型");
    for (const [model, spec] of Object.entries(state.models)) {
      if (spec.provider === id) select.append(new Option(spec.name, model));
    }
    const test = button("测试连接", async () => {
      test.disabled = true;
      test.textContent = "测试中…";
      failure.textContent = "";
      try {
        await api("test", { provider: id, model: select.value });
        toast("连接成功 · " + state.models[select.value].name);
      } catch (error) {
        failure.textContent = error.message;
      } finally {
        test.textContent = "测试连接";
        test.disabled = label.textContent === "未配置";
      }
    });
    function setStatus(status) {
      state.providers[id].status = status;
      label.textContent = { missing: "未配置", saved: "已保存", env: "来自环境变量" }[status];
      label.className = status === "missing" ? "muted" : "success";
      test.disabled = status === "missing";
      remove.hidden = status !== "saved";
      if (task) renderModels($("model").value, $("effort").value);
    }
    entry.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      failure.textContent = "";
      try {
        const value = input.value.trim();
        const { status } = await api("key", { provider: id, key: value });
        input.value = "";
        setStatus(status);
        if (task && state.providers[state.models[$("model").value]?.provider]?.status === "missing") {
          renderModels(select.value, null);
        }
        toast("API Key 已保存");
      } catch (error) {
        failure.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    entry.append(input, save, remove);
    testing.append(select, test);
    row.append(heading, entry, testing, failure);
    root.append(row);
    setStatus(provider.status);
  }
}

function openKeys() {
  keyRows();
  openDialog("keys-dialog");
}

function renderPlugins() {
  const root = $("plugin-rows");
  root.replaceChildren();
  for (const plugin of state.plugins) {
    const line = make("div", "plugin-row");
    const name = plugin.name === "builtin-text" ? "内置文本工具" : plugin.name;
    const labels = { connecting: "正在连接", connected: "已连接", error: "连接失败" };
    line.append(make("strong", "", name + " · " + labels[plugin.state]));
    if (plugin.tools.length) line.append(make("div", "muted", plugin.tools.join(" · ")));
    if (plugin.error) line.append(make("div", "error", plugin.error));
    root.append(line);
  }
}

async function openPlugins() {
  const config = await api("plugins");
  $("plugin-config").value = JSON.stringify(config, null, 2);
  $("plugin-error").textContent = "";
  renderPlugins();
  openDialog("plugins-dialog");
}

function toggleInspector(visible) {
  $("inspector").hidden = !visible;
  document.querySelector(".layout").classList.toggle("inspector-hidden", !visible);
  $("activity-toggle").setAttribute("aria-expanded", String(visible));
}

function tabs(selected) {
  for (const name of ["activity", "changes"]) {
    $(name + "-tab").setAttribute("aria-selected", String(name === selected));
    $(name + "-pane").hidden = name !== selected;
  }
}

for (const element of document.querySelectorAll("[data-close]")) {
  element.addEventListener("click", () => $(element.dataset.close).close());
}

$("new-task").addEventListener("click", openProject);
$("welcome-new").addEventListener("click", openProject);
$("settings-button").addEventListener("click", openKeys);
$("welcome-settings").addEventListener("click", openKeys);
$("plugins-button").addEventListener("click", () => watch(openPlugins()));
$("activity-toggle").addEventListener("click", () => toggleInspector($("inspector").hidden));
for (const name of ["activity", "changes"]) $(name + "-tab").addEventListener("click", () => tabs(name));
$("model").addEventListener("change", () => renderEffort(null));
$("prompt-form").addEventListener("submit", (event) => {
  event.preventDefault();
  watch(startPrompt());
});
$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
    event.preventDefault();
    if (!$("send").disabled) watch(startPrompt());
  }
});
$("stop").addEventListener("click", () => {
  if (live?.id) watch(api("stop", { run: live.id }).then(() => poll()));
});
$("pick-folder").addEventListener("click", async () => {
  const pick = $("pick-folder");
  pick.disabled = true;
  try {
    const { path } = await api("picker", {});
    if (path) $("project-path").value = path;
  } catch (error) {
    $("project-error").textContent = error.message;
  } finally {
    pick.disabled = false;
  }
});
$("project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.submitter;
  const errorLabel = $("project-error");
  errorLabel.textContent = "";
  if (submit) submit.disabled = true;
  try {
    const next = await api("task", { root: $("project-path").value });
    state = await api("state");
    setTask(next);
    $("project-dialog").close();
    $("prompt").focus();
  } catch (error) {
    errorLabel.textContent = error.message;
  } finally {
    if (submit) submit.disabled = false;
  }
});
$("welcome-demo").addEventListener("click", () => watch((async () => {
  const demoButton = $("welcome-demo");
  demoButton.disabled = true;
  try {
    const next = await api("task", { demo: true });
    state = await api("state");
    setTask(next);
    $("prompt").value = "把示例项目的问候语改成中文，运行单元测试，并试用内置 MCP 插件。";
    await startPrompt();
  } finally {
    demoButton.disabled = false;
  }
})()));
$("plugin-example").addEventListener("click", () => watch((async () => {
  const raw = JSON.parse($("plugin-config").value);
  raw.mcpServers ??= {};
  raw.mcpServers.example ??= { enabled: false, url: "https://mcp.example.com/mcp" };
  $("plugin-config").value = JSON.stringify(raw, null, 2);
})()));
$("plugin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const detail = $("plugin-error");
  detail.textContent = "";
  if (event.submitter) event.submitter.disabled = true;
  try {
    await api("plugins", JSON.parse($("plugin-config").value));
    await refresh();
    toast("配置已保存，正在连接启用的服务器");
  } catch (error) {
    detail.textContent = error.message;
  } finally {
    if (event.submitter) event.submitter.disabled = false;
  }
});

async function boot() {
  state = await api("state");
  const last = localStorage.getItem("active-task");
  const active = state.active || state.tasks.find((entry) => entry.id === last)?.id;
  toggleInspector(!window.matchMedia("(max-width: 770px)").matches);
  if (active) await openTask(active);
  else setTask(null);
  setInterval(() => {
    if (live || state.active) poll().catch(() => {});
  }, 350);
  setInterval(() => { refresh().catch(() => {}); }, 4500);
}

watch(boot());
