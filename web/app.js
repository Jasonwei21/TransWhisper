/* 语音转文字 GUI —— Vue 3 前端逻辑（离线单文件，无构建步骤） */
"use strict";

const STATUS_TEXT = {
  pending: "等待", running: "处理中", done: "完成", skip: "跳过", fail: "失败",
};

const SYSTEM_SEGMENTS = ["$recycle.bin", "recycler", "recycled",
                         "system volume information", "recovery"];

function isSystemPath(p) {
  return String(p).split(/[\\/]/).some((seg) => SYSTEM_SEGMENTS.includes(seg.toLowerCase()));
}

function makeInput(path) {
  const name = path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
  return { path, name, isDir: !/\.[a-z0-9]+$/i.test(name) };
}

async function api(method, url, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const resp = await fetch(url, opt);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

const { createApp } = Vue;

createApp({
  data() {
    return {
      // 健康信息 / 模型
      models: [],
      model: "",
      defaultModel: "",
      cpuCores: 0,
      defaultThreads: 0,
      // 任务设置
      language: "auto",
      prompt: "",
      formats: ["srt", "txt"],
      threads: "0",
      workers: "1",
      translate: false,
      gpu: true,
      gpuSaved: false,
      flashAttn: false,
      overwrite: false,
      flat: false,
      advOpen: false,
      // 输入 / 输出
      inputs: [],
      pathDraft: "",
      outputDir: "",
      outputDirDraft: "",
      // 运行状态
      healthState: "connecting",   // connecting | ok | err
      running: false,
      tasks: [],
      total: 0,
      jobState: "",
      serverLog: [],
      localLog: [],
      autoScroll: true,
      metrics: null,
      sysInfoText: "正在检测系统配置…",
      gpuHint: "",
      gpuHintWarn: false,
      // 使用说明（按钮打开的 Markdown 阅读器）与版本
      version: "",
      docOpen: false,
      readmeHtml: "",
      readmeSource: "",
      // 内部
      snack: { show: false, text: "" },
      snackTimer: null,
      pollTimer: null,
      polling: false,
      lastFp: "",
    };
  },

  computed: {
    healthText() {
      if (this.healthState === "connecting") return "连接中…";
      if (this.healthState === "err") return "服务连接失败";
      return `${this.models.length} 个模型 · ${this.cpuCores} 核 CPU`;
    },
    modelHint() {
      const name = this.model || "";
      if (name.includes("medium")) return "medium：中文表现接近 large，速度快一倍，推荐日常使用";
      if (name.includes("small")) return "small：速度最快，适合草稿或长音频粗转";
      if (name.includes("large")) return "large-v3：最准，内存约 3~4GB，长音频较慢";
      return "large-v3 最准但最慢";
    },
    threadsHint() {
      return this.defaultThreads ? `0 = 自动（推荐 ${this.defaultThreads}）` : "0 = 自动（推荐）";
    },
    countsText() {
      const total = this.total || 0;
      if (!total) return "待开始";
      const files = this.tasks;
      const finished = files.filter((f) => f.status !== "pending" && f.status !== "running").length;
      const runningN = files.filter((f) => f.status === "running").length;
      const doneN = files.filter((f) => f.status === "done").length;
      const failN = files.filter((f) => f.status === "fail").length;
      const skipN = files.filter((f) => f.status === "skip").length;
      const stateText = { running: "进行中", done: "已完成", error: "出错", cancelled: "已取消" }[this.jobState] || this.jobState;
      return `${stateText} · ${finished}/${total}` +
        (doneN ? ` · 成功 ${doneN}` : "") +
        (skipN ? ` · 跳过 ${skipN}` : "") +
        (failN ? ` · 失败 ${failN}` : "") +
        (runningN ? ` · 处理中 ${runningN}` : "");
    },
    progressPct() {
      const total = this.total || 0;
      if (!total) return "0%";
      const finished = this.tasks.filter((f) => f.status !== "pending" && f.status !== "running").length;
      return `${Math.round((finished / total) * 100)}%`;
    },
    visibleTasks() {
      return this.tasks.slice(0, 200);
    },
    taskMore() {
      return this.tasks.length > 200 ? `仅显示前 200 条，共 ${this.tasks.length} 条` : "";
    },
    logText() {
      const head = this.serverLog.length ? this.serverLog : ["(等待任务)"];
      return [...head, ...this.localLog].join("\n");
    },
    estimateText() {
      if (!this.inputs.length) return "";
      return `已选择 ${this.inputs.length} 项，开始后会自动扫描其中的音视频文件`;
    },
  },

  watch: {
    model() { this.saveSettings(); },
    language() { this.saveSettings(); },
    prompt() { this.saveSettings(); },
    formats: { handler() { this.saveSettings(); }, deep: true },
    threads() { this.saveSettings(); },
    workers() { this.saveSettings(); },
    translate() { this.saveSettings(); },
    gpu() { this.saveSettings(); },
    flashAttn() { this.saveSettings(); },
    overwrite() { this.saveSettings(); },
    flat() { this.saveSettings(); },
    logText() {
      this.$nextTick(() => {
        const el = this.$refs.logBox;
        if (el && this.autoScroll) el.scrollTop = el.scrollHeight;
      });
    },
  },

  mounted() {
    this.loadSettings();
    this.init();
    this.loadReadme();
    this._onVis = () => {
      if (!document.hidden) { this.poll(); this.schedulePoll(); }
    };
    document.addEventListener("visibilitychange", this._onVis);
    this._onKey = (e) => {
      if (e.key === "Escape") this.docOpen = false;
    };
    document.addEventListener("keydown", this._onKey);
  },

  beforeUnmount() {
    document.removeEventListener("visibilitychange", this._onVis);
    document.removeEventListener("keydown", this._onKey);
    clearTimeout(this.pollTimer);
    clearTimeout(this.snackTimer);
  },

  methods: {
    /* ================= 初始化 ================= */
    async init() {
      try {
        const h = await api("GET", "/api/health");
        this.models = h.models || [];
        this.defaultModel = h.default_model || "";
        this.cpuCores = h.cpu || 0;
        this.defaultThreads = h.default_threads || 0;
        this.version = h.version || "1.0.0";
        document.title = `语音转文字 v${this.version}`;
        this.healthState = "ok";

        if (!this.model || !this.models.some((m) => m.name === this.model)) {
          this.model = h.models.some((m) => m.name === this.defaultModel)
            ? this.defaultModel
            : (this.models[0] && this.models[0].name) || "";
        }

        // 结果目录（可自定义，存在服务端 config.json，换界面不丢）
        this.outputDir = h.results_dir || "";
        this.outputDirDraft = this.outputDir;

        // 系统信息与 GPU 策略
        const sys = h.system || {};
        const gpuNames = (sys.gpus || []).filter((g) => !/virtual/i.test(g));
        this.sysInfoText =
          `${sys.physical || this.cpuCores} 核 CPU · ${sys.ram_gb || "?"}GB 内存 · 显卡：${gpuNames.join(" / ") || "未知"}`;
        if (!this.gpuSaved) this.gpu = sys.gpu_recommended !== false;
        this.gpuHint = sys.gpu_note || "已启用 GPU 加速。若感到鼠标卡顿，请在高级设置里关掉它。";
        this.gpuHintWarn = sys.gpu_recommended === false;
      } catch (e) {
        this.healthState = "err";
        this.addLocalLog(`无法连接服务：${e.message}`);
      }
      this.poll();          // 立即拉一次状态，监控数据不用等首个轮询周期
      this.schedulePoll();
    },

    /* ================= 轮询 ================= */
    schedulePoll() {
      // 运行中快轮询；空闲放慢；浏览器标签页隐藏时几乎不打扰
      clearTimeout(this.pollTimer);
      const delay = document.hidden ? 15000 : (this.running ? 1000 : 8000);
      this.pollTimer = setTimeout(async () => {
        await this.poll();
        this.schedulePoll();
      }, delay);
    },

    async poll() {
      if (this.polling) return;
      this.polling = true;
      try {
        const st = await api("GET", "/api/status");
        this.renderStatus(st);
      } catch (_) {
        /* 服务暂时不可达，忽略 */
      } finally {
        this.polling = false;
      }
    },

    renderStatus(st) {
      this.running = st.state === "running";
      this.metrics = st.metrics || null;

      const files = st.files || [];
      const logArr = st.log || [];

      // 数据没变化就不重绘，避免浏览器被高频更新拖垮
      const fp = [
        st.state, st.total, logArr.length,
        logArr.length ? logArr[logArr.length - 1] : "",
        files.map((f) => `${f.status}|${f.elapsed}|${f.error || ""}`).join(","),
      ].join("~");
      if (fp === this.lastFp) return;
      this.lastFp = fp;

      this.jobState = st.state;
      this.total = st.total || 0;
      this.tasks = files;
      this.serverLog = logArr;
    },

    /* ================= 系统自带文件选择框（真实路径，无上传） ================= */
    async pickAndAdd(mode) {
      try {
        const r = await api("POST", "/api/pick", { mode });
        if (!r.paths || !r.paths.length) return;   // 用户取消
        for (const p of r.paths) this.addInput(p, mode === "dir");
        this.addLocalLog(`已添加 ${r.paths.length} 个${mode === "dir" ? "文件夹" : "文件"}：${r.paths[0]}${r.paths.length > 1 ? " 等" : ""}`);
      } catch (e) {
        this.notify(`调用系统文件选择框失败：${e.message}`);
      }
    },

    async pickOutputDir() {
      try {
        const r = await api("POST", "/api/pick", { mode: "dir" });
        if (r.paths && r.paths[0]) await this.setOutputDir(r.paths[0]);
      } catch (e) {
        this.notify(`调用系统文件选择框失败：${e.message}`);
      }
    },

    addFromDraft() {
      const v = this.pathDraft.trim();
      if (v) this.addInput(v, false);
      this.pathDraft = "";
    },

    addInput(path, isDir) {
      if (isSystemPath(path)) {
        this.notify(`该路径位于回收站/系统目录，已忽略：${path}`);
        return;
      }
      const item = makeInput(path);
      item.isDir = isDir;
      if (this.inputs.some((i) => i.path.toLowerCase() === path.toLowerCase())) return;
      this.inputs.push(item);
      this.saveSettings();
    },

    removeInput(idx) {
      this.inputs.splice(idx, 1);
      this.saveSettings();
    },

    clearInputs() {
      this.inputs = [];
      this.saveSettings();
    },

    async setOutputDir(path) {
      if (!path) return;
      if (isSystemPath(path)) {
        this.notify("不能把结果目录设在回收站/系统目录");
        this.outputDirDraft = this.outputDir;
        return;
      }
      try {
        const r = await api("POST", "/api/settings", { results_dir: path });
        this.outputDir = r.results_dir;
        this.outputDirDraft = this.outputDir;
      } catch (e) {
        this.notify(`设置失败：${e.message}`);
        this.outputDirDraft = this.outputDir;
      }
    },

    applyOutputDir() {
      const v = this.outputDirDraft.trim();
      if (v !== this.outputDir) this.setOutputDir(v);
    },

    /* ================= 任务 ================= */
    async startJob() {
      if (!this.inputs.length) { this.notify("请先选择输入文件或文件夹"); return; }
      const formats = this.formats.slice();
      if (!formats.length) { this.notify("请至少选择一种输出格式（SRT 或 TXT）"); return; }

      const payload = {
        inputs: this.inputs.map((i) => i.path),
        model: this.model,
        language: this.language,
        formats,
        prompt: this.prompt.trim(),
        threads: Number(this.threads || 0),
        workers: Number(this.workers || 1),
        translate: this.translate,
        no_gpu: !this.gpu,
        flash_attn: this.flashAttn,
        overwrite: this.overwrite,
        flat: this.flat,
      };

      try {
        await api("POST", "/api/start", payload);
        this.running = true;
        this.localLog = [];
        this.addLocalLog("任务已提交，正在扫描文件…");
        this.poll();
      } catch (e) {
        this.notify(`启动失败：${e.message}`);
      }
    },

    async stopJob() {
      try {
        await api("POST", "/api/stop");
        this.addLocalLog("已发送停止指令…");
      } catch (e) {
        this.notify(`停止失败：${e.message}`);
      }
    },

    async openResultsFolder() {
      try {
        await api("POST", "/api/open-folder");
        this.addLocalLog("已在资源管理器中打开结果文件夹");
      } catch (e) {
        this.notify(`打开失败：${e.message}`);
      }
    },

    /* ================= 设置持久化 ================= */
    saveSettings() {
      const data = {
        inputs: this.inputs.map((i) => i.path),
        model: this.model,
        language: this.language,
        prompt: this.prompt,
        formats: this.formats.length ? this.formats : ["srt", "txt"],
        threads: this.threads,
        workers: this.workers,
        translate: this.translate,
        gpu: this.gpu,
        flash_attn: this.flashAttn,
        overwrite: this.overwrite,
        flat: this.flat,
      };
      try { localStorage.setItem("asr-gui-settings", JSON.stringify(data)); } catch (_) {}
    },

    loadSettings() {
      try {
        const data = JSON.parse(localStorage.getItem("asr-gui-settings") || "null");
        if (!data) return;
        const rawInputs = (data.inputs || []).map((p) => makeInput(p));
        this.inputs = rawInputs.filter((i) => !isSystemPath(i.path));
        if (this.inputs.length !== rawInputs.length) {
          // 清掉了历史保存的回收站/系统目录路径
          setTimeout(() => this.saveSettings(), 0);
        }
        if (data.language) this.language = data.language;
        if (data.prompt) this.prompt = data.prompt;
        if (data.threads !== undefined) this.threads = String(data.threads);
        if (data.workers !== undefined) this.workers = String(data.workers);
        this.translate = !!data.translate;
        if (data.gpu !== undefined) {
          this.gpu = !!data.gpu;
          this.gpuSaved = true;
        } else if (data.no_gpu !== undefined) {
          this.gpu = !data.no_gpu;
          this.gpuSaved = true;
        }
        this.flashAttn = !!data.flash_attn;
        this.overwrite = !!data.overwrite;
        this.flat = !!data.flat;
        if (data.model) this.model = data.model;
        if (data.formats && data.formats.length) this.formats = data.formats.slice();
      } catch (_) {}
    },

    /* ================= 使用说明（内建 Markdown 阅读器） ================= */
    async loadReadme() {
      try {
        const r = await api("GET", "/api/readme");
        this.readmeSource = r.source || "";
        this.readmeHtml = r.markdown
          ? this.renderMarkdown(r.markdown)
          : '<p class="hint">未找到 README.md / README.txt，使用说明暂不可用。</p>';
      } catch (e) {
        this.readmeHtml = `<p class="hint">使用说明加载失败：${this.escapeHtml(e.message)}</p>`;
      }
    },

    renderMarkdown(md) {
      // marked（本地 vendor，离线可用）；缺失时退回纯文本
      if (!(window.marked && marked.parse)) {
        return `<pre class="md-plain">${this.escapeHtml(md)}</pre>`;
      }
      let html = marked.parse(String(md), { gfm: true, breaks: false });

      // 为每个标题注入 GitHub 风格锚点 id，目录链接才能跳转
      const used = {};
      html = html.replace(/<h([1-6])([^>]*)>([\s\S]*?)<\/h\1>/gi, (m, lv, attrs, inner) => {
        const text = inner.replace(/<[^>]+>/g, "").trim();
        let slug = this.githubSlug(text) || "section";
        if (used[slug]) { used[slug] += 1; slug = `${slug}-${used[slug]}`; }
        else used[slug] = 0;
        return `<h${lv}${attrs} id="${this.escapeHtml(slug)}">${inner}</h${lv}>`;
      });
      return html;
    },

    githubSlug(text) {
      // GitHub 锚点规则：小写、去标点、空格转连字符（中文保留）
      return String(text).trim().toLowerCase()
        .replace(/[^\p{L}\p{N}\s-]/gu, "")
        .replace(/\s+/g, "-");
    },

    onMdClick(e) {
      // 使用说明里的链接：目录锚点滚动、外链新标签、相对路径给提示
      const a = e.target && e.target.closest ? e.target.closest("a") : null;
      if (!a) return;
      const href = a.getAttribute("href") || "";
      if (href.startsWith("#")) {
        e.preventDefault();
        const id = decodeURIComponent(href.slice(1));
        const el = document.getElementById(id);
        const box = document.querySelector(".dialog-body");
        if (el && box) {
          const y = el.getBoundingClientRect().top -
                    box.getBoundingClientRect().top + box.scrollTop - 12;
          box.scrollTo({ top: y, behavior: "smooth" });
          el.classList.remove("flash");
          void el.offsetWidth;               // 重新触发高亮动画
          el.classList.add("flash");
          setTimeout(() => el.classList.remove("flash"), 1400);
        }
      } else if (/^https?:/i.test(href)) {
        e.preventDefault();
        window.open(href, "_blank", "noopener");
      } else if (href) {
        e.preventDefault();
        this.notify(`请在工具目录中直接打开该文件：${href}`);
      }
    },

    escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[c]));
    },

    /* ================= 小工具 ================= */
    statusText(s) { return STATUS_TEXT[s] || s; },
    shortModel(name) {
      return String(name).replace("ggml-", "").replace(".bin", "");
    },
    fmtTemp(t) {
      return (t === null || t === undefined) ? "--°C" : `${t}°C`;
    },
    fmtPct(p) {
      return (p === null || p === undefined) ? "--%" : `${p}%`;
    },
    tempClass(t) {
      if (t === null || t === undefined) return "na";
      return t >= 80 ? "hot" : t >= 65 ? "warm" : "";
    },
    pctClass(p, hotAt, warmAt) {
      if (p === null || p === undefined) return "na";
      return p >= hotAt ? "hot" : p >= warmAt ? "warm" : "";
    },
    addLocalLog(text) {
      const ts = new Date().toTimeString().slice(0, 8);
      this.localLog.push(`[${ts}] ${text}`);
    },
    notify(text) {
      this.snack = { show: true, text };
      clearTimeout(this.snackTimer);
      this.snackTimer = setTimeout(() => { this.snack.show = false; }, 4200);
    },
  },
}).mount("#app");
