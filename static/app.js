const app = {
  state: {
    hosts: [], settings: {}, groups: [], profiles: [], jobs: [], summary: {},
    gallery: [], gallerySignature: "",
    galleryFilters: { query: "", workflow: "", type: "", sort: "newest" },
    currentProfile: null, values: new Map(), enabled: new Set(), showAdvanced: false,
    pendingUploadKey: null, pendingPasteKey: null, taskTabs: [], activeTaskTabId: null, taskTabCounter: 0,
    currentMetadata: null, previewItems: [], currentPreviewIndex: -1,
  },

  commonFields: new Set([
    "text", "prompt", "user_prompt", "negative_prompt", "seed", "steps", "cfg", "denoise",
    "sampler_name", "scheduler", "width", "height", "aspect_ratio", "megapixels",
    "multiple", "batch_size", "duration", "filename_prefix", "image", "video", "audio", "file",
  ]),

  titles: {
    create: ["WORKFLOW CONSOLE", "创建任务"],
    queue: ["SERIAL EXECUTION", "任务队列"],
    results: ["LOCAL GALLERY", "结果图库"],
    settings: ["LOCAL PREFERENCES", "设置"],
  },

  async api(path, options = {}) {
    const response = await fetch(path, {
      method: options.method || "GET",
      headers: options.body ? { "Content-Type": "application/json" } : {},
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`本地服务返回异常（HTTP ${response.status}）`); }
    if (!response.ok || data.ok === false) {
      const detail = data.details?.msg || data.error || `请求失败（HTTP ${response.status}）`;
      throw new Error(detail);
    }
    return data;
  },

  escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, ch => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[ch]);
  },

  toast(message, type = "ok") {
    const el = document.createElement("div");
    el.className = `toast ${type === "error" ? "error" : ""}`;
    el.textContent = message;
    document.querySelector("#toastStack").append(el);
    setTimeout(() => el.remove(), 4200);
  },

  setBusy(button, busy, label = "处理中…") {
    if (!button) return;
    if (busy) {
      button.dataset.original = button.textContent;
      button.textContent = label;
      button.disabled = true;
    } else {
      button.textContent = button.dataset.original || button.textContent;
      button.disabled = false;
    }
  },

  async loadState(initial = false) {
    try {
      const [data, gallery] = await Promise.all([
        this.api("/api/state"),
        this.api("/api/gallery"),
      ]);
      const activeProfile = this.state.currentProfile;
      Object.assign(this.state, data);
      this.state.gallery = gallery.items || [];
      if (activeProfile) {
        this.state.currentProfile = activeProfile._restored
          ? activeProfile
          : (this.state.profiles.find(profile => profile.id === activeProfile.id) || activeProfile);
      }
      if (initial) {
        document.querySelector("#hostSelect").value = data.settings.default_host || "ai";
        document.querySelector("#workflowIdInput").value = data.settings.default_workflow_id || "";
        document.querySelector("#autoDownloadCheck").checked = data.settings.auto_download !== false;
        document.querySelector("#sendWorkflowCheck").checked = Boolean(data.settings.send_full_workflow);
        document.querySelector("#downloadDirInput").value = data.settings.download_dir || "";
        document.querySelector("#pollIntervalInput").value = data.settings.poll_interval || 5;
        document.querySelector("#timeoutInput").value = data.settings.timeout_minutes || 60;
        this.renderProfiles();
        if (data.profiles.length) {
          const preferred = data.profiles.find(p => p.workflowId === data.settings.default_workflow_id) || data.profiles[0];
          this.createTaskTab({ profile: preferred });
        } else {
          this.createTaskTab();
        }
      } else {
        this.renderProfileOptions(this.state.currentProfile?.id || "");
      }
      this.renderConnection();
      this.renderQueue();
      this.renderResults();
      this.renderSettingsProfiles();
    } catch (error) {
      this.toast(error.message, "error");
    }
  },

  navigate(view) {
    document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === view));
    document.querySelectorAll(".view").forEach(el => el.classList.toggle("active", el.id === `view-${view}`));
    const [eyebrow, title] = this.titles[view];
    document.querySelector("#pageEyebrow").textContent = eyebrow;
    document.querySelector("#pageTitle").textContent = title;
    if (view === "results") { this.renderResults(); this.layoutResultGrid(); }
  },

  hostInfo(hostId) {
    return this.state.hosts.find(h => h.id === hostId) || { id: hostId, domain: hostId, hasKey: false };
  },

  renderConnection() {
    const host = document.querySelector("#hostSelect").value;
    const info = this.hostInfo(host);
    const chip = document.querySelector("#connectionChip");
    chip.textContent = info.hasKey ? `${info.domain} · Key 已保存` : `${info.domain} · 未保存 Key`;
    chip.classList.toggle("connected", info.hasKey);
    for (const item of this.state.hosts) {
      const status = document.querySelector(`#${item.id}KeyState`);
      if (status) status.textContent = item.hasKey ? "已安全保存" : "未保存";
    }
    document.querySelector("#keyStoreLabel").textContent = this.state.keyStore || "本地存储";
  },

  renderProfiles() {
    this.renderProfileOptions(this.state.currentProfile?.id || "");
    this.renderSettingsProfiles();
  },

  editorState(profile, overrides = []) {
    const values = new Map();
    const enabled = new Set();
    if (profile) {
      this.editableFields(profile.workflow).forEach(field => {
        values.set(field.key, field.value);
        if (field.common) enabled.add(field.key);
      });
      overrides.forEach(item => {
        const key = `${item.nodeId}::${item.fieldName}`;
        values.set(key, item.fieldValue);
        enabled.add(key);
      });
    }
    return { values, enabled };
  },

  captureActiveTaskTab() {
    const tab = this.state.taskTabs.find(item => item.id === this.state.activeTaskTabId);
    if (!tab) return;
    tab.profile = this.state.currentProfile;
    tab.values = new Map(this.state.values);
    tab.enabled = new Set(this.state.enabled);
    tab.showAdvanced = this.state.showAdvanced;
    tab.host = document.querySelector("#hostSelect").value;
    tab.workflowId = document.querySelector("#workflowIdInput").value.trim();
    tab.runs = document.querySelector("#runsInput").value;
    tab.seedMode = document.querySelector("#seedModeSelect").value;
    tab.seedValue = document.querySelector("#seedValueInput").value;
    tab.seedStep = document.querySelector("#seedStepInput").value;
    tab.autoDownload = document.querySelector("#autoDownloadCheck").checked;
    tab.sendFullWorkflow = document.querySelector("#sendWorkflowCheck").checked;
  },

  createTaskTab(options = {}) {
    this.captureActiveTaskTab();
    const profile = options.profile || null;
    const editor = this.editorState(profile, options.overrides || []);
    const seedField = profile ? this.editableFields(profile.workflow).find(field => field.fieldName === "seed") : null;
    const id = `task-tab-${Date.now()}-${++this.state.taskTabCounter}`;
    const tab = {
      id, profile, values: editor.values, enabled: editor.enabled,
      showAdvanced: false,
      host: options.host || profile?.host || this.state.settings.default_host || "ai",
      workflowId: options.workflowId || profile?.workflowId || "",
      title: options.title || profile?.name || `新任务 ${this.state.taskTabCounter}`,
      restoredSource: options.restoredSource || "",
      runs: "1", seedMode: options.seed !== undefined && options.seed !== null ? "fixed" : "random",
      seedValue: options.seed ?? seedField?.value ?? "", seedStep: "1",
      autoDownload: this.state.settings.auto_download !== false,
      sendFullWorkflow: Boolean(this.state.settings.send_full_workflow),
    };
    this.state.taskTabs.push(tab);
    this.activateTaskTab(id, false);
  },

  activateTaskTab(id, capture = true) {
    if (capture && id !== this.state.activeTaskTabId) this.captureActiveTaskTab();
    const tab = this.state.taskTabs.find(item => item.id === id);
    if (!tab) return;
    this.state.activeTaskTabId = id;
    this.state.currentProfile = tab.profile;
    this.state.values = new Map(tab.values);
    this.state.enabled = new Set(tab.enabled);
    this.state.showAdvanced = tab.showAdvanced;
    document.querySelector("#hostSelect").value = tab.host;
    document.querySelector("#workflowIdInput").value = tab.workflowId;
    document.querySelector("#runsInput").value = tab.runs;
    document.querySelector("#seedModeSelect").value = tab.seedMode;
    document.querySelector("#seedValueInput").value = tab.seedValue;
    document.querySelector("#seedStepInput").value = tab.seedStep;
    document.querySelector("#autoDownloadCheck").checked = tab.autoDownload;
    document.querySelector("#sendWorkflowCheck").checked = tab.sendFullWorkflow;
    this.renderProfileOptions(tab.profile?.id || "");
    this.renderCurrentEditor(tab.restoredSource);
    this.renderTaskTabs();
  },

  closeTaskTab(id) {
    const index = this.state.taskTabs.findIndex(item => item.id === id);
    if (index < 0) return;
    const wasActive = id === this.state.activeTaskTabId;
    this.state.taskTabs.splice(index, 1);
    if (!this.state.taskTabs.length) {
      this.state.activeTaskTabId = null;
      this.createTaskTab();
      return;
    }
    if (wasActive) {
      const next = this.state.taskTabs[Math.min(index, this.state.taskTabs.length - 1)];
      this.state.activeTaskTabId = null;
      this.activateTaskTab(next.id, false);
    } else {
      this.renderTaskTabs();
    }
  },

  renderTaskTabs() {
    const container = document.querySelector("#taskTabs");
    if (!container) return;
    container.innerHTML = this.state.taskTabs.map(tab => `<div class="task-tab ${tab.id === this.state.activeTaskTabId ? "active" : ""}" role="presentation">
      <button class="task-tab-main" role="tab" aria-selected="${tab.id === this.state.activeTaskTabId}" data-task-tab="${this.escape(tab.id)}" title="${this.escape(tab.title)}">${tab.restoredSource ? "↻ " : ""}${this.escape(tab.title)}</button>
      <button class="task-tab-close" data-close-task-tab="${this.escape(tab.id)}" title="关闭标签">×</button>
    </div>`).join("");
    container.querySelectorAll("[data-task-tab]").forEach(button => button.addEventListener("click", () => this.activateTaskTab(button.dataset.taskTab)));
    container.querySelectorAll("[data-close-task-tab]").forEach(button => button.addEventListener("click", () => this.closeTaskTab(button.dataset.closeTaskTab)));
  },

  renderCurrentEditor(restoredSource = "") {
    const profile = this.state.currentProfile;
    if (!profile) {
      document.querySelector("#profileSelect").value = "";
      document.querySelector("#workflowMeta").textContent = "读取后会自动展示所有可调参数；节点连线不会被修改。";
      document.querySelector("#parameterGroups").classList.add("hidden");
      document.querySelector("#emptyParams").classList.remove("hidden");
      this.renderConnection();
      return;
    }
    if (this.state.profiles.some(item => item.id === profile.id)) document.querySelector("#profileSelect").value = profile.id;
    const stamp = profile.updatedAt ? ` · ${profile.updatedAt.replace("T", " ")}` : "";
    document.querySelector("#workflowMeta").textContent = `${profile.name} · ${Object.keys(profile.workflow).length} 个节点${stamp}${restoredSource ? ` · 来源：${restoredSource}` : ""}`;
    document.querySelector("#parameterGroups").classList.remove("hidden");
    document.querySelector("#emptyParams").classList.add("hidden");
    this.renderParams();
    this.renderConnection();
  },

  resetCurrentParams() {
    const profile = this.state.currentProfile;
    if (!profile) return;
    const editor = this.editorState(profile);
    this.state.values = editor.values;
    this.state.enabled = editor.enabled;
    const seed = this.editableFields(profile.workflow).find(field => field.fieldName === "seed");
    document.querySelector("#seedValueInput").value = seed?.value ?? "";
    this.renderParams();
    this.toast("已恢复此标签的原始参数");
  },

  renderProfileOptions(selectedId = "") {
    const select = document.querySelector("#profileSelect");
    const current = selectedId || select.value;
    const options = profiles => profiles.map(profile =>
      `<option value="${this.escape(profile.id)}">${this.escape(profile.name)} · ${this.escape(profile.workflowId)}</option>`
    ).join("");
    const ungrouped = this.state.profiles.filter(profile => !profile.groupId);
    const grouped = this.state.groups.map(group => {
      const profiles = this.state.profiles.filter(profile => profile.groupId === group.id);
      return profiles.length ? `<optgroup label="${this.escape(group.name)}">${options(profiles)}</optgroup>` : "";
    }).join("");
    select.innerHTML = `<option value="">请选择或读取新工作流</option>` +
      (ungrouped.length ? `<optgroup label="未分组">${options(ungrouped)}</optgroup>` : "") + grouped;
    if (this.state.profiles.some(p => p.id === current)) select.value = current;
  },

  selectProfile(profileId) {
    const profile = this.state.profiles.find(p => p.id === profileId);
    this.state.currentProfile = profile || null;
    const editor = this.editorState(profile);
    this.state.values = editor.values;
    this.state.enabled = editor.enabled;
    this.state.showAdvanced = false;
    if (profile) {
      document.querySelector("#hostSelect").value = profile.host;
      document.querySelector("#workflowIdInput").value = profile.workflowId;
    }
    const seed = profile ? this.editableFields(profile.workflow).find(f => f.fieldName === "seed") : null;
    document.querySelector("#seedValueInput").value = seed?.value ?? "";
    const tab = this.state.taskTabs.find(item => item.id === this.state.activeTaskTabId);
    if (tab) {
      tab.profile = profile || null;
      tab.title = profile?.name || tab.title;
      tab.restoredSource = "";
    }
    this.renderCurrentEditor();
    this.renderTaskTabs();
  },

  editableFields(workflow) {
    const fields = [];
    for (const [nodeId, node] of Object.entries(workflow || {})) {
      const inputs = node?.inputs || {};
      for (const [fieldName, value] of Object.entries(inputs)) {
        const scalar = value === null || ["string", "number", "boolean"].includes(typeof value);
        if (!scalar) continue;
        const semanticName = this.semanticFieldName(node, fieldName, workflow, nodeId, value);
        fields.push({
          key: `${nodeId}::${fieldName}`, nodeId, fieldName, semanticName, value,
          type: value === null ? "string" : typeof value,
          nodeTitle: node?._meta?.title || node?.class_type || `节点 ${nodeId}`,
          classType: node?.class_type || "Unknown",
          common: this.commonFields.has(semanticName),
          fileLike: /(^|_)(image|video|audio|file|path)($|_)/i.test(fieldName),
          imageLike: /(^|_)(image|images)($|_)/i.test(fieldName) || /LoadImage/i.test(node?.class_type || ""),
        });
      }
    }
    const priority = ["user_prompt", "text", "prompt", "negative_prompt", "duration", "seed", "steps", "cfg", "denoise", "sampler_name", "scheduler", "aspect_ratio", "megapixels", "width", "height", "multiple", "batch_size"];
    fields.sort((a, b) => {
      const ai = priority.indexOf(a.semanticName), bi = priority.indexOf(b.semanticName);
      if (ai !== bi) return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
      return Number(a.nodeId) - Number(b.nodeId) || a.fieldName.localeCompare(b.fieldName);
    });
    return fields;
  },

  semanticFieldName(node, fieldName, workflow = {}, nodeId = "", value = null) {
    if (fieldName === "value" && /^Primitive(?:Float|Int|String|Boolean)/i.test(node?.class_type || "")) {
      const title = String(node?._meta?.title || "");
      const match = title.match(/\(([^()]+)\)\s*$/);
      return match?.[1]?.trim().toLowerCase().replace(/[\s-]+/g, "_") || fieldName;
    }
    if (typeof value !== "string") return fieldName;

    const normalizedField = fieldName.trim().toLowerCase().replace(/[\s-]+/g, "_");
    const textCarrier = ["text", "prompt", "user_prompt", "custom_prompt", "negative_prompt", "编辑文本", "文本", "提示词"].includes(normalizedField);
    if (!textCarrier) return fieldName;

    const consumers = [];
    for (const target of Object.values(workflow || {})) {
      for (const [targetField, targetValue] of Object.entries(target?.inputs || {})) {
        if (Array.isArray(targetValue) && String(targetValue[0]) === String(nodeId)) consumers.push(targetField);
      }
    }
    const hints = `${normalizedField} ${node?._meta?.title || ""} ${consumers.join(" ")}`.toLowerCase();
    if (/negative[ _-]*prompt|负面.*提示词/.test(hints)) return "negative_prompt";
    if (/custom[ _-]*prompt|user[ _-]*prompt|用户.*提示词/.test(hints)) return "user_prompt";
    if (/prompt|提示词/.test(hints)) return "prompt";
    if (/编辑文本|(^|[\s_-])text([\s_-]|$)|(^|\s)文本(\s|$)/i.test(hints)) return "text";
    return fieldName;
  },

  humanField(field) {
    const names = {
      text: "提示词", prompt: "提示词", user_prompt: "用户提示词", negative_prompt: "负面提示词", seed: "随机种子",
      steps: "采样步数", cfg: "CFG 引导", denoise: "降噪强度", sampler_name: "采样器",
      scheduler: "调度器", aspect_ratio: "画面比例", megapixels: "百万像素",
      width: "宽度", height: "高度", duration: "视频时长（秒）", multiple: "生成数量", batch_size: "批次数量",
      filename_prefix: "文件名前缀", image: "输入图片", video: "输入视频", audio: "输入音频",
    };
    return names[field.semanticName] || field.semanticName;
  },

  mediaPreviewUrl(value) {
    if (!value) return "";
    const host = document.querySelector("#hostSelect")?.value || this.state.currentProfile?.host || "ai";
    return `/api/media-preview?host=${encodeURIComponent(host)}&name=${encodeURIComponent(String(value))}`;
  },

  renderParams() {
    const profile = this.state.currentProfile;
    if (!profile) return;
    const all = this.editableFields(profile.workflow);
    const common = all.filter(f => f.common);
    const advanced = all.filter(f => !f.common);
    const renderGroup = (label, fields, advancedGroup = false) => {
      if (!fields.length || (advancedGroup && !this.state.showAdvanced)) return "";
      return `<section class="param-section ${advancedGroup ? "advanced-group" : ""}">
        <div class="param-section-title"><span>${label}</span><span>${fields.length} 项</span></div>
        <div class="param-grid">${fields.map(field => this.paramCard(field)).join("")}</div>
      </section>`;
    };
    document.querySelector("#parameterGroups").innerHTML =
      renderGroup("常用参数", common) + renderGroup("其他可编辑字段", advanced, true);
    this.bindParamEvents();
    document.querySelector("#toggleAdvancedButton").textContent = this.state.showAdvanced ? "隐藏高级" : `显示全部（${advanced.length}）`;
  },

  paramCard(field) {
    const enabled = this.state.enabled.has(field.key);
    const value = this.state.values.get(field.key);
    const promptLike = ["text", "prompt", "user_prompt", "negative_prompt"].includes(field.semanticName);
    let control;
    if (field.type === "boolean") {
      control = `<label class="bool-control"><input class="param-value" data-key="${this.escape(field.key)}" type="checkbox" ${value ? "checked" : ""}><span>${value ? "已开启" : "已关闭"}</span></label>`;
    } else if (promptLike) {
      control = `<textarea class="param-value" data-key="${this.escape(field.key)}">${this.escape(value)}</textarea>
        <button class="text-button clean-prompt" data-key="${this.escape(field.key)}">整理空格与标点</button>`;
    } else {
      const type = field.type === "number" ? "number" : "text";
      const step = field.type === "number" ? "any" : undefined;
      const input = `<input class="param-value" data-key="${this.escape(field.key)}" type="${type}" ${step ? `step="${step}"` : ""} value="${this.escape(value)}">`;
      const fileControl = `<div class="upload-inline">${input}<button class="upload-field" data-key="${this.escape(field.key)}">更换${field.imageLike ? "图片" : "文件"}</button>${field.imageLike ? `<button class="paste-field" data-paste-key="${this.escape(field.key)}" title="点击后按 ⌘V 粘贴剪贴板图片">粘贴图片</button><button class="gallery-pick-field" data-gallery-pick-key="${this.escape(field.key)}" title="从结果图库中选择一张图片">从图库选择</button>` : ""}</div>`;
      if (field.fileLike && field.imageLike) {
        const previewUrl = this.mediaPreviewUrl(value);
        control = `<div class="image-input-control">
          <div class="input-image-preview ${previewUrl ? "" : "unavailable"}" data-image-preview>
            ${previewUrl ? `<img src="${this.escape(previewUrl)}" alt="${this.escape(field.nodeTitle)} 的输入图片">` : ""}
            <div class="image-preview-empty"><span>▧</span><b>暂无本地预览</b><small>选择文件或点「粘贴图片」后按 ⌘V</small></div>
          </div>
          ${fileControl}
        </div>`;
      } else {
        control = field.fileLike ? fileControl : input;
      }
    }
    return `<article class="param-card ${promptLike ? "prompt-card" : ""} ${enabled ? "" : "disabled-field"}" data-card-key="${this.escape(field.key)}">
      <div class="param-top">
        <div class="param-title"><b>${this.escape(this.humanField(field))}</b><small>${this.escape(field.nodeTitle)} · ${this.escape(field.nodeId)}.${this.escape(field.fieldName)}</small></div>
        <input class="field-toggle" type="checkbox" data-toggle-key="${this.escape(field.key)}" ${enabled ? "checked" : ""} title="是否提交此字段">
      </div>
      ${control}
    </article>`;
  },

  bindParamEvents() {
    document.querySelectorAll(".field-toggle").forEach(el => el.addEventListener("change", event => {
      const key = event.target.dataset.toggleKey;
      if (event.target.checked) this.state.enabled.add(key); else this.state.enabled.delete(key);
      document.querySelector(`[data-card-key="${CSS.escape(key)}"]`)?.classList.toggle("disabled-field", !event.target.checked);
    }));
    document.querySelectorAll(".param-value").forEach(el => {
      const eventName = el.type === "checkbox" ? "change" : "input";
      el.addEventListener(eventName, event => {
        const key = event.target.dataset.key;
        const field = this.editableFields(this.state.currentProfile.workflow).find(f => f.key === key);
        let value = event.target.type === "checkbox" ? event.target.checked : event.target.value;
        if (field?.type === "number" && value !== "") value = Number(value);
        this.state.values.set(key, value);
        this.state.enabled.add(key);
        const toggle = document.querySelector(`[data-toggle-key="${CSS.escape(key)}"]`);
        if (toggle) toggle.checked = true;
        document.querySelector(`[data-card-key="${CSS.escape(key)}"]`)?.classList.remove("disabled-field");
        if (field?.fieldName === "seed") document.querySelector("#seedValueInput").value = value;
        if (event.target.type === "checkbox") event.target.nextElementSibling.textContent = value ? "已开启" : "已关闭";
      });
    });
    document.querySelectorAll(".clean-prompt").forEach(button => button.addEventListener("click", () => {
      const key = button.dataset.key;
      const textarea = document.querySelector(`.param-value[data-key="${CSS.escape(key)}"]`);
      let value = textarea.value.trim().replace(/[ \t]+/g, " ").replace(/\s*,\s*/g, ", ").replace(/,{2,}/g, ",");
      value = value.replace(/\n{3,}/g, "\n\n");
      textarea.value = value;
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      this.toast("提示词格式已整理");
    }));
    document.querySelectorAll(".upload-field").forEach(button => button.addEventListener("click", () => {
      this.state.pendingUploadKey = button.dataset.key;
      this.state.pendingPasteKey = null;
      const field = this.editableFields(this.state.currentProfile.workflow).find(item => item.key === button.dataset.key);
      const input = document.querySelector("#parameterFileInput");
      input.accept = field?.imageLike ? "image/*" : field?.fieldName.includes("video") ? "video/*" : field?.fieldName.includes("audio") ? "audio/*" : "";
      input.click();
    }));
    document.querySelectorAll(".paste-field").forEach(button => button.addEventListener("click", () => {
      this.state.pendingPasteKey = button.dataset.pasteKey;
      this.state.pendingUploadKey = null;
      document.querySelectorAll(".paste-field").forEach(el => el.classList.toggle("waiting", el === button));
      this.toast("已选中该字段，现在按 ⌘V 粘贴剪贴板图片");
    }));
    document.querySelectorAll(".gallery-pick-field").forEach(button => button.addEventListener("click", () => {
      this.openGalleryPicker(button.dataset.galleryPickKey);
    }));
    document.querySelectorAll("[data-image-preview] img").forEach(img => {
      img.addEventListener("load", () => img.parentElement.classList.add("loaded"));
      img.addEventListener("error", () => img.parentElement.classList.add("unavailable"));
      img.addEventListener("click", () => {
        if (img.parentElement.classList.contains("loaded")) this.preview(img.src, "png");
      });
      if (img.complete) {
        img.parentElement.classList.add(img.naturalWidth ? "loaded" : "unavailable");
      }
    });
  },

  collectOverrides() {
    if (!this.state.currentProfile) return [];
    const fieldMap = new Map(this.editableFields(this.state.currentProfile.workflow).map(f => [f.key, f]));
    return [...this.state.enabled].map(key => {
      const field = fieldMap.get(key);
      return { nodeId: field.nodeId, fieldName: field.fieldName, fieldValue: this.state.values.get(key) };
    });
  },

  async fetchWorkflow() {
    const host = document.querySelector("#hostSelect").value;
    const workflowId = document.querySelector("#workflowIdInput").value.trim();
    if (!this.hostInfo(host).hasKey) {
      this.toast("请先在设置中保存这个站点的 API Key", "error");
      this.navigate("settings");
      return;
    }
    if (!workflowId) return this.toast("请输入 Workflow ID", "error");
    const current = this.state.currentProfile;
    const defaultName = current?.name || `工作流 ${workflowId.slice(-7)}`;
    this.state.currentPreviewIndex = -1;
    document.querySelector("#modalContent").innerHTML = `<div class="workflow-fetch-dialog">
      <span>REFRESH WORKFLOW</span>
      <h2>如何保存读取到的工作流？</h2>
      <p>将从 <b>${this.escape(this.hostInfo(host).domain)}</b> 读取 Workflow ${this.escape(workflowId)} 的最新版本。</p>
      <label><span>新工作流名称</span><input id="remoteWorkflowNameInput" maxlength="120" value="${this.escape(defaultName)}"></label>
      <div class="workflow-fetch-actions">
        <button class="secondary-button" data-fetch-mode="overwrite" ${current ? "" : "disabled"}>覆盖当前工作流<br><small>${current ? `保留名称“${this.escape(current.name)}”` : "当前没有可覆盖的工作流"}</small></button>
        <button class="primary-button" data-fetch-mode="new">另存为新工作流<br><small>使用上面的名称</small></button>
      </div>
    </div>`;
    document.querySelector("#previewModal").classList.remove("hidden");
    document.querySelectorAll("[data-fetch-mode]").forEach(button => button.addEventListener("click", () => {
      this.performRemoteFetch(button.dataset.fetchMode, host, workflowId, button);
    }));
    setTimeout(() => document.querySelector("#remoteWorkflowNameInput")?.focus(), 0);
  },

  async performRemoteFetch(mode, host, workflowId, button) {
    const current = this.state.currentProfile;
    const name = document.querySelector("#remoteWorkflowNameInput")?.value.trim();
    if (mode === "new" && !name) return this.toast("请输入新工作流名称", "error");
    this.setBusy(button, true, "读取中…");
    try {
      const data = await this.api("/api/profiles/remote", {
        method: "POST", body: mode === "overwrite"
          ? { host, workflowId, id: current.id, name: current.name }
          : { host, workflowId, name, groupId: current?.groupId || null, createNew: true },
      });
      this.state.profiles = [data.profile, ...this.state.profiles.filter(p => p.id !== data.profile.id)];
      this.renderProfiles();
      this.selectProfile(data.profile.id);
      document.querySelector("#previewModal").classList.add("hidden");
      this.toast(`${mode === "overwrite" ? "已覆盖当前工作流" : "已另存为新工作流"}：${data.nodeCount} 个节点`);
    } catch (error) { this.toast(error.message, "error"); }
    finally { this.setBusy(button, false); }
  },

  async importWorkflow(file) {
    try {
      const workflow = JSON.parse(await file.text());
      const workflowId = document.querySelector("#workflowIdInput").value.trim();
      if (!workflowId) throw new Error("导入前请填写 Workflow ID");
      const data = await this.api("/api/profiles/import", {
        method: "POST",
        body: {
          host: document.querySelector("#hostSelect").value,
          workflowId, name: file.name.replace(/\.json$/i, ""), workflow,
        },
      });
      this.state.profiles = [data.profile, ...this.state.profiles.filter(p => p.id !== data.profile.id)];
      this.renderProfiles();
      this.selectProfile(data.profile.id);
      this.toast(`已导入 ${Object.keys(workflow).length} 个节点`);
    } catch (error) { this.toast(error.message, "error"); }
  },

  imageFieldKeys() {
    if (!this.state.currentProfile) return [];
    return this.editableFields(this.state.currentProfile.workflow)
      .filter(field => field.imageLike && field.fileLike)
      .map(field => field.key);
  },

  targetPasteKey() {
    // 1) 用户点过「粘贴图片」按钮的字段优先。
    if (this.state.pendingPasteKey) return this.state.pendingPasteKey;
    // 2) 焦点在某个图片参数输入框里，粘贴到该字段。
    const active = document.activeElement;
    if (active?.dataset?.key) {
      const field = this.editableFields(this.state.currentProfile.workflow).find(f => f.key === active.dataset.key);
      if (field?.imageLike && field.fileLike) return field.key;
    }
    // 3) 当前工作流只有一个图片输入字段时，直接填入它。
    const keys = this.imageFieldKeys();
    return keys.length === 1 ? keys[0] : null;
  },

  async handleClipboardPaste(event) {
    const items = [...(event.clipboardData?.items || [])];
    const imageItem = items.find(item => item.kind === "file" && item.type.startsWith("image/"));
    if (!imageItem) return;
    const key = this.targetPasteKey();
    if (!key) {
      const count = this.imageFieldKeys().length;
      this.toast(count > 1
        ? `剪贴板里有图片，但当前工作流有 ${count} 个图片字段。请先点击目标参数下的「粘贴图片」按钮再按 ⌘V`
        : "请先读取一个包含图片输入的工作流，或点击「粘贴图片」按钮后再按 ⌘V", "error");
      return;
    }
    event.preventDefault();
    const blob = imageItem.getAsFile();
    if (!blob) return this.toast("无法读取剪贴板中的图片", "error");
    const ext = blob.type === "image/jpeg" ? "jpg" : blob.type === "image/webp" ? "webp" : blob.type === "image/gif" ? "gif" : "png";
    const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
    const file = new File([blob], `clipboard-${stamp}.${ext}`, { type: blob.type || "image/png" });
    this.state.pendingUploadKey = key;
    this.state.pendingPasteKey = null;
    await this.uploadParameterFile(file);
  },

  galleryImageItems() {
    const items = [...(this.state.gallery || [])].filter(item => {
      const type = String(item.fileType || (item.url || "").split(".").pop() || "").toLowerCase();
      return /^(png|jpg|jpeg|webp|gif|avif|bmp)$/.test(type) && item.url;
    });
    return items.sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  },

  openGalleryPicker(key) {
    const items = this.galleryImageItems();
    const modal = document.querySelector("#previewModal");
    const content = document.querySelector("#modalContent");
    if (!items.length) {
      content.innerHTML = `<div class="gallery-picker"><div class="gallery-picker-head"><h2>从结果图库选择</h2><span>0 张图片</span></div><p class="gallery-picker-empty">图库里还没有可用的图片结果。先运行任务，或在图库页点「扫描本地文件」。</p></div>`;
      modal.classList.remove("hidden");
      return;
    }
    const card = item => `<button class="gallery-pick-card" data-pick-id="${this.escape(item.id)}" title="${this.escape(item.fileName || "")}">
      <img src="${this.escape(item.url)}" loading="lazy" alt="${this.escape(item.fileName || "")}">
      <small>${this.escape((item.createdAt || "").slice(5, 16).replace("T", " "))}</small>
    </button>`;
    content.innerHTML = `<div class="gallery-picker">
      <div class="gallery-picker-head"><h2>从结果图库选择</h2><span>${items.length} 张图片 · 点击即可填入参数</span></div>
      <div class="gallery-pick-grid">${items.map(card).join("")}</div>
    </div>`;
    modal.classList.remove("hidden");
    content.querySelectorAll("[data-pick-id]").forEach(button => button.addEventListener("click", () => {
      const item = items.find(entry => String(entry.id) === button.dataset.pickId);
      if (item) this.pickGalleryImage(key, item);
    }));
  },

  async pickGalleryImage(key, item) {
    document.querySelector("#previewModal").classList.add("hidden");
    this.toast("正在读取图片…");
    try {
      const response = await fetch(item.hasLocal ? item.localUrl || item.url : item.url);
      if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`);
      let blob = await response.blob();
      if (!blob.type.startsWith("image/")) blob = new Blob([blob], { type: "image/png" });
      const name = item.fileName || `gallery-${item.id}.png`;
      const file = new File([blob], name, { type: blob.type });
      this.state.pendingUploadKey = key;
      this.state.pendingPasteKey = null;
      await this.uploadParameterFile(file);
    } catch (error) {
      this.toast(item.hasLocal
        ? `无法读取本地图片：${error.message}`
        : `远程图片无法直接读取（可能受跨域限制），请先在图库中保存到本地再选择：${error.message}`, "error");
    }
  },

  async uploadParameterFile(file) {
    const key = this.state.pendingUploadKey;
    if (!key || !file) return;
    if (file.size > 64 * 1024 * 1024) return this.toast("文件不能超过 64MB", "error");
    this.toast(`正在上传 ${file.name}…`);
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const result = await this.api("/api/upload", {
        method: "POST",
        body: {
          host: document.querySelector("#hostSelect").value,
          filename: file.name, contentType: file.type, dataBase64: dataUrl,
        },
      });
      const value = result.data?.fileName || result.data?.filename;
      if (!value) throw new Error("上传响应中没有 fileName");
      this.state.values.set(key, value);
      this.state.enabled.add(key);
      this.renderParams();
      this.toast("文件已上传并填入节点参数");
    } catch (error) { this.toast(error.message, "error"); }
    finally {
      this.state.pendingUploadKey = null;
      this.state.pendingPasteKey = null;
      document.querySelectorAll(".paste-field.waiting").forEach(el => el.classList.remove("waiting"));
      const input = document.querySelector("#parameterFileInput");
      input.value = "";
      input.accept = "";
    }
  },

  async enqueue() {
    const profile = this.state.currentProfile;
    if (!profile) return this.toast("请先读取或导入工作流", "error");
    const button = document.querySelector("#enqueueButton");
    const runs = Math.max(1, Math.min(200, Number(document.querySelector("#runsInput").value) || 1));
    this.setBusy(button, true, "正在加入…");
    try {
      const body = {
        host: document.querySelector("#hostSelect").value,
        workflowId: document.querySelector("#workflowIdInput").value.trim(),
        profileId: profile.id, profileName: profile.name,
        workflow: profile.workflow, overrides: this.collectOverrides(), runs,
        seedMode: document.querySelector("#seedModeSelect").value,
        seedValue: document.querySelector("#seedValueInput").value,
        seedStep: document.querySelector("#seedStepInputInput")?.value || document.querySelector("#seedStepInput").value,
        autoDownload: document.querySelector("#autoDownloadCheck").checked,
        sendFullWorkflow: document.querySelector("#sendWorkflowCheck").checked,
      };
      await this.api("/api/queue", { method: "POST", body });
      this.toast(`已加入 ${runs} 个串行任务`);
      await this.loadState(false);
      this.navigate("queue");
    } catch (error) { this.toast(error.message, "error"); }
    finally { this.setBusy(button, false); }
  },

  renderQueue() {
    const jobs = this.state.jobs || [];
    const summary = this.state.summary || {};
    document.querySelector("#summaryQueued").textContent = summary.queued || 0;
    document.querySelector("#summaryRunning").textContent = summary.running || 0;
    document.querySelector("#summarySuccess").textContent = summary.success || 0;
    document.querySelector("#summaryFailed").textContent = summary.failed || 0;
    document.querySelector("#queueBadge").textContent = (summary.queued || 0) + (summary.running || 0);
    document.querySelector("#liveCount").textContent = (summary.queued || 0) + (summary.running || 0);
    const paused = Boolean(this.state.settings.queue_paused);
    document.querySelector("#pauseQueueButton").textContent = paused ? "继续队列" : "暂停队列";
    document.querySelector("#queuePauseButton").textContent = paused ? "继续队列" : "暂停队列";

    const activeJobs = jobs.filter(j => ["RUNNING", "SUBMITTING"].includes(j.status));
    document.querySelector("#workerState").textContent = paused ? "队列已暂停" : activeJobs.length ? `${activeJobs.length} 个任务执行中` : (summary.queued ? "等待执行" : "队列空闲");
    document.querySelector("#workerDetail").textContent = activeJobs.length
      ? activeJobs.map(job => `.${job.host} ${job.profileName} · ${job.runIndex}/${job.runTotal}`).join(" ｜ ")
      : "双站并行 · 站内串行";
    document.querySelector("#liveJob").innerHTML = activeJobs.length
      ? activeJobs.map(job => `<div class="live-job"><strong>.${this.escape(job.host)} · ${this.escape(job.profileName)}</strong><span>${this.escape(job.message || job.status)}</span><div class="progress-track"><i></i></div></div>`).join("")
      : `<div class="live-empty">暂无运行中的任务</div>`;

    const tbody = document.querySelector("#jobsTableBody");
    tbody.innerHTML = jobs.map(job => {
      const canCancel = ["QUEUED", "RUNNING", "SUBMITTING"].includes(job.status);
      const canRetry = ["FAILED", "CANCELLED", "SUCCESS"].includes(job.status);
      const canDownload = job.status === "SUCCESS" && job.results.some(r => !r.localPath);
      return `<tr>
        <td><span class="status-pill status-${this.escape(job.status)}">${this.statusLabel(job.status)}</span></td>
        <td><strong>${this.escape(job.profileName)}</strong><small>第 ${job.runIndex}/${job.runTotal} 次 · ${this.escape(job.message || "")}</small>${job.error ? `<small title="${this.escape(job.error)}">${this.escape(job.error).slice(0, 90)}</small>` : ""}</td>
        <td><strong>.${this.escape(job.host)}</strong><small>${this.escape(job.workflowId)}</small></td>
        <td><code>${this.escape(job.remoteTaskId || "—")}</code></td>
        <td><small>${this.escape(job.updatedAt.replace("T", " "))}</small></td>
        <td><div class="row-actions">
          ${canDownload ? `<button data-job-action="download" data-job-id="${job.id}">保存</button>` : ""}
          ${canRetry ? `<button data-job-action="retry" data-job-id="${job.id}">重试</button>` : ""}
          ${canCancel ? `<button data-job-action="cancel" data-job-id="${job.id}">取消</button>` : ""}
        </div></td>
      </tr>`;
    }).join("");
    document.querySelector("#jobsEmpty").classList.toggle("hidden", jobs.length > 0);
    document.querySelector(".table-wrap").classList.toggle("hidden", jobs.length === 0);
    tbody.querySelectorAll("[data-job-action]").forEach(button => button.addEventListener("click", () => this.jobAction(button)));
  },

  statusLabel(status) {
    return ({ QUEUED: "等待", SUBMITTING: "提交中", RUNNING: "执行中", SUCCESS: "完成", FAILED: "失败", CANCELLED: "已取消" })[status] || status;
  },

  async jobAction(button) {
    const action = button.dataset.jobAction;
    const id = button.dataset.jobId;
    this.setBusy(button, true);
    try {
      await this.api(`/api/jobs/${id}/${action}`, { method: "POST", body: {} });
      this.toast(({ cancel: "取消请求已发送", retry: "已加入重试任务", download: "结果已保存" })[action]);
      await this.loadState(false);
    } catch (error) { this.toast(error.message, "error"); }
    finally { this.setBusy(button, false); }
  },

  galleryTypeOf(item) {
    const type = String(item.fileType || (item.url || "").split(".").pop() || "file").toLowerCase();
    if (/^(png|jpg|jpeg|webp|gif|avif|bmp)$/.test(type)) return "image";
    if (/^(mp4|mov|webm|m4v)$/.test(type)) return "video";
    if (/^(mp3|wav|m4a|ogg|flac)$/.test(type)) return "audio";
    if (this.isTextType(type, item.url)) return "text";
    return "other";
  },

  applyGalleryFilters(items) {
    const filters = this.state.galleryFilters;
    let list = items.slice();
    if (filters.workflow) list = list.filter(item => (item.profileName || "") === filters.workflow);
    if (filters.type) list = list.filter(item => this.galleryTypeOf(item) === filters.type);
    const query = filters.query.trim().toLowerCase();
    if (query) {
      list = list.filter(item => {
        const haystack = item.searchText || [item.fileName, item.profileName, item.jobId, item.remoteTaskId].join(" ");
        return String(haystack || "").toLowerCase().includes(query);
      });
    }
    const byCreated = (a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || ""));
    if (filters.sort === "oldest") list.sort((a, b) => -byCreated(a, b));
    else if (filters.sort === "name") list.sort((a, b) => String(a.fileName || "").localeCompare(String(b.fileName || ""), "zh-Hans-CN"));
    else if (filters.sort === "workflow") list.sort((a, b) => String(a.profileName || "").localeCompare(String(b.profileName || ""), "zh-Hans-CN") || byCreated(a, b));
    else list.sort(byCreated);
    return list;
  },

  syncGalleryWorkflowOptions(items) {
    const select = document.querySelector("#galleryWorkflowFilter");
    if (!select) return;
    const names = Array.from(new Set(items.map(item => item.profileName || "").filter(Boolean))).sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
    const next = JSON.stringify(names);
    if (next === select.dataset.options) return;
    select.dataset.options = next;
    const current = this.state.galleryFilters.workflow;
    select.innerHTML = `<option value="">全部工作流</option>` + names.map(name =>
      `<option value="${this.escape(name)}">${this.escape(name)}</option>`).join("");
    select.value = names.includes(current) ? current : "";
    this.state.galleryFilters.workflow = select.value;
  },

  renderResults(force = false) {
    const all = this.state.gallery || [];
    const items = this.applyGalleryFilters(all);
    // Skip the rebuild while nothing changed: the grid would reload every
    // image and text preview on each poll otherwise.
    const signature = JSON.stringify(this.state.galleryFilters) + "\n" + items.map(item =>
      [item.id, item.hasLocal ? "L" : "R", item.fileMissing ? "!" : "", item.fileName, item.profileName].join("|")
    ).join("\n");
    if (!force && signature === this.state.gallerySignature) return;
    this.state.gallerySignature = signature;

    this.syncGalleryWorkflowOptions(all);
    const filtersActive = Boolean(this.state.galleryFilters.query || this.state.galleryFilters.workflow || this.state.galleryFilters.type);
    document.querySelector("#resultsCount").textContent = all.length
      ? (filtersActive ? `${items.length} / ${all.length} 个结果` : `${items.length} 个结果`) : "";
    const empty = document.querySelector("#resultsEmpty");
    empty.classList.toggle("hidden", items.length > 0);
    document.querySelector("#resultsEmptyTitle").textContent = all.length && filtersActive ? "没有符合筛选条件的结果" : "结果将在这里出现";
    document.querySelector("#resultsEmptyHint").textContent = all.length && filtersActive ? "换个关键词，或点右侧「重置」清除筛选。" : "图片、视频和音频会自动分类展示。";
    const grid = document.querySelector("#resultGrid");
    this.state.previewItems = items.map(item => {
      const type = String(item.fileType || (item.url || "").split(".").pop() || "file").toLowerCase();
      return {
        src: item.url, type, textUrl: item.textUrl,
        jobId: item.jobId, resultIndex: item.resultIndex, profileName: item.profileName,
      };
    });
    grid.innerHTML = items.map((item, galleryIndex) => {
      const type = String(item.fileType || (item.url || "").split(".").pop() || "file").toLowerCase();
      const textType = this.isTextType(type, item.url);
      const media = this.mediaHtml(item.url, type, false, item.textUrl);
      const imageType = /^(png|jpg|jpeg|webp|gif|avif|bmp)$/.test(type);
      return `<article class="result-card">
        <div class="result-media" data-gallery-index="${galleryIndex}">
          ${media}<span class="media-type">${this.escape(type)}</span>${item.hasLocal ? `<span class="saved-badge">已保存</span>` : ""}${item.fileMissing ? `<span class="saved-badge missing">文件缺失</span>` : ""}
        </div>
        <div class="result-info"><strong>${this.escape(item.profileName)}</strong>
          <div class="result-meta"><span>${this.escape((item.createdAt || "").slice(0, 19).replace("T", " "))}</span><span>节点 ${this.escape(item.nodeId || "—")}</span></div>
          <div class="result-actions">
            <a href="${this.escape(item.url)}" target="_blank" rel="noopener">${textType ? "打开文本文件" : imageType ? "打开原图" : "打开原文件"}</a>
            ${item.hasLocal ? `<a href="${this.escape(item.localUrl)}" download>下载副本</a>` : `<button data-gallery-save="${this.escape(item.id)}">保存到本地</button>`}
            <button data-restore-gallery="${this.escape(item.id)}">复用参数</button>
            <button data-generation-info="${this.escape(item.id)}">生成信息</button>
            <button class="danger-text" data-gallery-forget="${this.escape(item.id)}">移出图库</button>
          </div>
        </div>
      </article>`;
    }).join("");
    this.hydrateTextPreviews(grid);
    grid.querySelectorAll("[data-gallery-index]").forEach(el => el.addEventListener("click", () => this.previewGallery(Number(el.dataset.galleryIndex))));
    grid.querySelectorAll("[data-gallery-save]").forEach(button => button.addEventListener("click", () => this.saveGalleryItem(button)));
    grid.querySelectorAll("[data-restore-gallery]").forEach(button => button.addEventListener("click", () => this.restoreResult(button)));
    grid.querySelectorAll("[data-generation-info]").forEach(button => button.addEventListener("click", () => this.showGenerationInfo(button)));
    grid.querySelectorAll("[data-gallery-forget]").forEach(button => button.addEventListener("click", () => this.forgetGalleryItem(button)));
    this.layoutResultGrid();
  },

  // Pinterest-style masonry: equal-width columns, each card placed into the
  // currently shortest column. With items in chronological order the first
  // cards fill the top row left to right, then later ones pack into the
  // shortest column, so reading order flows left to right and downward.
  async layoutResultGrid() {
    const grid = document.querySelector("#resultGrid");
    const cards = grid ? Array.from(grid.querySelectorAll(".result-card")) : [];
    if (!cards.length) { if (grid) grid.style.height = ""; return; }
    const ratios = await Promise.all(cards.map(card => new Promise(resolve => {
      const done = value => resolve(Number(value) > 0 ? Number(value) : 1.25);
      let settled = false;
      const finish = value => { if (!settled) { settled = true; done(value); } };
      const img = card.querySelector("img");
      const video = card.querySelector("video");
      if (img) {
        if (img.complete && img.naturalWidth) return finish(img.naturalWidth / img.naturalHeight);
        const probe = new Image();
        probe.onload = () => finish(probe.naturalWidth / probe.naturalHeight);
        probe.onerror = () => finish(1.25);
        probe.src = img.currentSrc || img.src;
      } else if (video) {
        if (video.videoWidth) return finish(video.videoWidth / video.videoHeight);
        video.addEventListener("loadedmetadata", () => finish(video.videoWidth / video.videoHeight), { once: true });
      } else {
        finish(1.5); // text / audio / unknown types
      }
      setTimeout(() => finish(1.25), 3000); // never hang on a stalled load
    })));
    const width = grid.clientWidth;
    if (!width) return;
    const GAP = 14;
    const IDEAL = 286; // preferred column width
    const cols = Math.max(1, Math.min(cards.length, Math.floor((width + GAP) / (IDEAL + GAP))));
    const colWidth = Math.floor((width - GAP * (cols - 1)) / cols);
    const colHeights = new Array(cols).fill(0);
    cards.forEach((card, i) => {
      const ratio = Math.min(Math.max(ratios[i], 0.5), 3); // clamp extreme panoramas/tall shots
      const height = Math.round(colWidth / ratio);
      let col = 0;
      for (let c = 1; c < cols; c++) if (colHeights[c] < colHeights[col]) col = c;
      card.style.width = `${colWidth}px`;
      card.style.height = `${height}px`;
      card.style.transform = `translate(${col * (colWidth + GAP)}px, ${colHeights[col]}px)`;
      card.classList.add("placed");
      colHeights[col] += height + GAP;
    });
    grid.style.height = `${Math.max(...colHeights) - GAP}px`;
  },

  async saveGalleryItem(button) {
    const id = button.dataset.gallerySave;
    this.setBusy(button, true, "下载中…");
    try {
      await this.api(`/api/gallery/${encodeURIComponent(id)}/save`, { method: "POST", body: {} });
      this.toast("结果已保存到本地");
      await this.loadState(false);
      this.renderResults(true);
    } catch (error) { this.toast(error.message, "error"); }
    finally { this.setBusy(button, false); }
  },

  async forgetGalleryItem(button) {
    const id = button.dataset.galleryForget;
    if (!confirm("把这个结果移出图库？\n只会删除图库索引，本地文件仍然保留在下载目录中。")) return;
    this.setBusy(button, true, "移除中…");
    try {
      await this.api(`/api/gallery/${encodeURIComponent(id)}`, { method: "DELETE" });
      this.toast("已移出图库（本地文件保留）");
      await this.loadState(false);
      this.renderResults(true);
    } catch (error) { this.toast(error.message, "error"); }
    finally { this.setBusy(button, false); }
  },

  async restoreResult(button) {
    this.setBusy(button, true, "读取中…");
    try {
      const id = button.dataset.restoreGallery || button.dataset.restoreJob;
      const path = button.dataset.restoreGallery
        ? `/api/gallery/${encodeURIComponent(id)}/restore`
        : `/api/jobs/${id}/restore?result=${button.dataset.resultIndex || 0}`;
      const data = await this.api(path);
      const restore = data.restore;
      const known = this.state.profiles.find(profile => profile.id === restore.profileId);
      const profile = {
        ...(known || {}), id: restore.profileId || `restored-${restore.sourceJobId}`,
        name: restore.name, host: restore.host, workflowId: restore.workflowId,
        workflow: restore.workflow, _restored: true,
      };
      this.createTaskTab({
        profile, overrides: restore.overrides, host: restore.host,
        workflowId: restore.workflowId, title: restore.name,
        restoredSource: restore.source, seed: restore.seed,
      });
      this.navigate("create");
      this.toast(`已从${restore.source}打开新标签`);
    } catch (error) { this.toast(error.message, "error"); }
    finally { this.setBusy(button, false); }
  },

  metadataValue(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "开启" : "关闭";
    return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  },

  metadataLabel(name) {
    return ({
      seed: "Seed", noise_seed: "Noise Seed", steps: "采样步数", cfg: "CFG",
      denoise: "降噪强度", sampler_name: "采样器", scheduler: "调度器",
      width: "宽度", height: "高度", aspect_ratio: "画面比例", megapixels: "百万像素",
      multiple: "生成数量", batch_size: "批次数量", duration: "视频时长（秒）",
      frame_rate: "帧率", crf: "视频 CRF", format: "输出格式",
      ckpt_name: "Checkpoint", checkpoint: "Checkpoint", checkpoint_name: "Checkpoint",
      unet_name: "扩散模型 / UNET", model_name: "模型", clip_name: "CLIP / 文本编码器",
      vae_name: "VAE", control_net_name: "ControlNet",
    })[name] || name;
  },

  async showGenerationInfo(button) {
    this.setBusy(button, true, "读取中…");
    try {
      const infoId = button.dataset.generationInfo;
      const data = await this.api(`/api/gallery/${encodeURIComponent(infoId)}/metadata`);
      const metadata = data.metadata;
      this.state.currentMetadata = metadata;
      this.state.currentPreviewIndex = -1;
      const fieldRows = items => items.map(item => `<div class="metadata-row">
        <span>${this.escape(this.metadataLabel(item.name))}<small>${this.escape(item.nodeTitle)} · ${this.escape(item.nodeId)}.${this.escape(item.fieldName)}</small></span>
        <code>${this.escape(this.metadataValue(item.value))}</code>
      </div>`).join("");
      const prompts = metadata.prompts.length ? metadata.prompts.map((item, index) => `<article class="metadata-prompt">
        <div><b>${item.kind === "negative" ? "负面 Prompt" : metadata.prompts.length > 1 ? `Prompt ${index + 1}` : "Prompt"}</b><small>${this.escape(item.nodeTitle)} · 节点 ${this.escape(item.nodeId)}</small></div>
        <pre>${this.escape(item.value)}</pre>
      </article>`).join("") : `<p class="metadata-empty">没有识别到 Prompt 字段</p>`;
      const loras = metadata.loras.length ? metadata.loras.map(item => `<div class="metadata-lora">
        <div><b>${this.escape(item.name)}</b><small>${this.escape(item.title)} · 节点 ${this.escape(item.nodeId)}</small></div>
        <span>模型权重 ${this.escape(this.metadataValue(item.strengthModel))}${item.strengthClip !== null && item.strengthClip !== undefined ? ` · CLIP ${this.escape(this.metadataValue(item.strengthClip))}` : ""}</span>
      </div>`).join("") : `<p class="metadata-empty">未使用或未识别到 LoRA</p>`;
      const raw = JSON.stringify({ workflow: metadata.workflow, overrides: metadata.overrides }, null, 2);
      document.querySelector("#modalContent").innerHTML = `<div class="metadata-sheet">
        <header class="metadata-header"><div><span>GENERATION METADATA</span><h2>${this.escape(metadata.profileName)}</h2><p>${this.escape(metadata.host)} · Workflow ${this.escape(metadata.workflowId)} · 来源：${this.escape(metadata.source)}</p></div>
          <div class="metadata-copy-actions"><button data-copy-metadata="prompt">复制 Prompt</button><button data-copy-metadata="json">复制完整 JSON</button></div>
        </header>
        <section class="metadata-section"><h3>提示词</h3>${prompts}</section>
        <div class="metadata-columns">
          <section class="metadata-section"><h3>模型与编码器</h3>${metadata.models.length ? fieldRows(metadata.models) : `<p class="metadata-empty">没有识别到模型字段</p>`}</section>
          <section class="metadata-section"><h3>LoRA</h3>${loras}</section>
        </div>
        <section class="metadata-section"><h3>主要生成参数</h3><div class="metadata-param-grid">${fieldRows(metadata.parameters)}</div></section>
        <details class="metadata-details"><summary>其他节点参数（${metadata.other.length}）</summary><div>${fieldRows(metadata.other)}</div></details>
        <details class="metadata-details"><summary>完整工作流 JSON</summary><pre>${this.escape(raw)}</pre></details>
        <footer>任务 ${this.escape(metadata.jobId)}${metadata.remoteTaskId ? ` · RunningHub ${this.escape(metadata.remoteTaskId)}` : ""} · ${this.escape(metadata.createdAt.replace("T", " "))}</footer>
      </div>`;
      document.querySelectorAll("[data-copy-metadata]").forEach(copyButton => copyButton.addEventListener("click", () => this.copyMetadata(copyButton.dataset.copyMetadata)));
      document.querySelector("#previewModal").classList.remove("hidden");
    } catch (error) { this.toast(error.message, "error"); }
    finally { this.setBusy(button, false); }
  },

  async copyMetadata(kind) {
    const metadata = this.state.currentMetadata;
    if (!metadata) return;
    const text = kind === "prompt"
      ? metadata.prompts.map(item => item.value).join("\n\n")
      : JSON.stringify({ workflow: metadata.workflow, overrides: metadata.overrides }, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      this.toast(kind === "prompt" ? "Prompt 已复制" : "完整生成信息已复制");
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = text; document.body.append(textarea); textarea.select();
      document.execCommand("copy"); textarea.remove();
      this.toast("已复制");
    }
  },

  isTextType(type, src = "") {
    return /^(txt|text|md|markdown|log|csv|json|plain)$/.test(type) || /\.(txt|md|markdown|log|csv|json)(?:$|[?#])/i.test(src);
  },

  mediaHtml(src, type, controls = false, textUrl = "") {
    if (/^(mp4|mov|webm|m4v)$/.test(type)) return `<video src="${this.escape(src)}" ${controls ? "controls autoplay" : "muted preload=metadata"}></video>`;
    if (/^(mp3|wav|m4a|ogg|flac)$/.test(type)) return `<audio src="${this.escape(src)}" controls></audio>`;
    if (/^(png|jpg|jpeg|webp|gif|avif|bmp)$/.test(type)) return `<img src="${this.escape(src)}" loading="lazy" alt="RunningHub 输出结果">`;
    if (this.isTextType(type, src)) return `<div class="text-result-preview ${controls ? "full" : ""}" data-text-source="${this.escape(textUrl)}">
      <div class="text-result-head"><b>TEXT OUTPUT</b>${controls ? `<button data-copy-result-text>复制全文</button>` : ""}</div>
      <pre>正在读取文本…</pre>
    </div>`;
    return `<div style="color:#bbb;font-size:12px">${this.escape(type.toUpperCase())} 文件</div>`;
  },

  hydrateTextPreviews(root = document) {
    root.querySelectorAll("[data-text-source]").forEach(async container => {
      if (container.dataset.loading) return;
      container.dataset.loading = "1";
      const pre = container.querySelector("pre");
      try {
        const data = await this.api(container.dataset.textSource);
        pre.textContent = data.text || "（文本结果为空）";
        container.dataset.fullText = data.text || "";
        container.classList.add("loaded");
        const copy = container.querySelector("[data-copy-result-text]");
        if (copy) copy.addEventListener("click", async event => {
          event.stopPropagation();
          try {
            await navigator.clipboard.writeText(container.dataset.fullText);
            this.toast("文本已复制");
          } catch { this.toast("复制失败，请手动选择文本", "error"); }
        });
      } catch (error) {
        pre.textContent = `文本预览失败：${error.message}`;
        container.classList.add("error");
      }
    });
  },

  preview(src, type) {
    if (!src) return;
    this.state.currentPreviewIndex = -1;
    const modal = document.querySelector("#previewModal");
    document.querySelector("#modalContent").innerHTML = this.mediaHtml(src, type, true);
    modal.classList.remove("hidden");
  },

  previewGallery(index) {
    if (!this.state.previewItems.length) return;
    this.state.currentPreviewIndex = Math.max(0, Math.min(index, this.state.previewItems.length - 1));
    this.renderGalleryPreview();
    document.querySelector("#previewModal").classList.remove("hidden");
  },

  renderGalleryPreview() {
    const total = this.state.previewItems.length;
    const index = this.state.currentPreviewIndex;
    if (index < 0 || !total) return;
    const item = this.state.previewItems[index];
    document.querySelector("#modalContent").innerHTML = `<div class="gallery-preview-shell">
      <div class="gallery-preview-media">${this.mediaHtml(item.src, item.type, true, item.textUrl)}</div>
      ${total > 1 ? `<button class="gallery-nav gallery-nav-prev" data-gallery-step="-1" aria-label="上一个结果">‹</button>
        <button class="gallery-nav gallery-nav-next" data-gallery-step="1" aria-label="下一个结果">›</button>` : ""}
      <div class="gallery-preview-caption"><span>${this.escape(item.profileName)}</span><b>${index + 1} / ${total}</b><small>← ↑ 上一个　→ ↓ 下一个</small></div>
    </div>`;
    document.querySelectorAll("[data-gallery-step]").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      this.stepGallery(Number(button.dataset.galleryStep));
    }));
    this.hydrateTextPreviews(document.querySelector("#modalContent"));
  },

  stepGallery(step) {
    const total = this.state.previewItems.length;
    if (this.state.currentPreviewIndex < 0 || total < 2) return;
    this.state.currentPreviewIndex = (this.state.currentPreviewIndex + step + total) % total;
    this.renderGalleryPreview();
  },

  renderSettingsProfiles() {
    const container = document.querySelector("#profileCards");
    if (!container) return;
    const query = (document.querySelector("#workflowSearchInput")?.value || "").trim().toLowerCase();
    const visible = this.state.profiles.filter(profile =>
      !query || profile.name.toLowerCase().includes(query) || profile.workflowId.toLowerCase().includes(query)
    );
    const groupOptions = selected => `<option value="">未分组</option>` + this.state.groups.map(group =>
      `<option value="${this.escape(group.id)}" ${selected === group.id ? "selected" : ""}>${this.escape(group.name)}</option>`
    ).join("");
    const card = profile => `<article class="profile-card">
      <div class="profile-card-head"><b title="${this.escape(profile.name)}">${this.escape(profile.name)}</b><span>.${this.escape(profile.host)}</span></div>
      <code>${this.escape(profile.workflowId)}</code>
      <small>${Object.keys(profile.workflow).length} 个节点 · 更新于 ${this.escape(profile.updatedAt.slice(0, 16).replace("T", " "))}</small>
      <label class="profile-group-select"><span>所属分组</span><select data-profile-group="${profile.id}">${groupOptions(profile.groupId)}</select></label>
      <div class="profile-card-actions"><button data-use-profile="${profile.id}">使用</button><button data-rename-profile="${profile.id}">改名</button><button class="danger-text" data-delete-profile="${profile.id}">删除</button></div>
    </article>`;
    const sections = [
      { id: "", name: "未分组", builtin: true },
      ...this.state.groups.map(group => ({ ...group, builtin: false })),
    ].map(group => {
      const profiles = visible.filter(profile => (profile.groupId || "") === group.id);
      if (!profiles.length && group.builtin) return "";
      return `<section class="workflow-group">
        <div class="workflow-group-header"><div><b>${this.escape(group.name)}</b><span>${profiles.length} 个工作流</span></div>
          ${group.builtin ? "" : `<div class="group-actions"><button data-rename-group="${group.id}">重命名</button><button class="danger-text" data-delete-group="${group.id}">删除分组</button></div>`}
        </div>
        ${profiles.length ? `<div class="profile-cards">${profiles.map(card).join("")}</div>` : `<div class="group-empty">这个分组还没有工作流</div>`}
      </section>`;
    }).join("");
    container.innerHTML = sections || `<div class="empty-state"><strong>${query ? "没有匹配的工作流" : "还没有保存工作流"}</strong><span>${query ? "换个关键词试试。" : "从创建页读取远程工作流或导入 JSON。"}</span></div>`;
    container.querySelectorAll("[data-use-profile]").forEach(button => button.addEventListener("click", () => {
      this.selectProfile(button.dataset.useProfile); this.navigate("create");
    }));
    container.querySelectorAll("[data-rename-profile]").forEach(button => button.addEventListener("click", () => this.renameProfile(button.dataset.renameProfile)));
    container.querySelectorAll("[data-delete-profile]").forEach(button => button.addEventListener("click", () => this.deleteProfile(button.dataset.deleteProfile)));
    container.querySelectorAll("[data-profile-group]").forEach(select => select.addEventListener("change", () => this.updateProfile(select.dataset.profileGroup, { groupId: select.value || null }, "分组已更新")));
    container.querySelectorAll("[data-rename-group]").forEach(button => button.addEventListener("click", () => this.renameGroup(button.dataset.renameGroup)));
    container.querySelectorAll("[data-delete-group]").forEach(button => button.addEventListener("click", () => this.deleteGroup(button.dataset.deleteGroup)));
  },

  async updateProfile(id, patch, message) {
    try {
      await this.api(`/api/profiles/${id}`, { method: "PATCH", body: patch });
      await this.loadState(false);
      this.toast(message);
    } catch (error) { this.toast(error.message, "error"); }
  },

  renameProfile(id) {
    const profile = this.state.profiles.find(item => item.id === id);
    if (!profile) return;
    const name = window.prompt("新的工作流名称", profile.name);
    if (name === null || !name.trim() || name.trim() === profile.name) return;
    this.updateProfile(id, { name: name.trim() }, "工作流名称已更新");
  },

  async createGroup() {
    const input = document.querySelector("#newGroupInput");
    const name = input.value.trim();
    if (!name) return this.toast("请输入分组名称", "error");
    try {
      await this.api("/api/groups", { method: "POST", body: { name } });
      input.value = "";
      await this.loadState(false);
      this.toast("分组已创建");
    } catch (error) { this.toast(error.message, "error"); }
  },

  renameGroup(id) {
    const group = this.state.groups.find(item => item.id === id);
    if (!group) return;
    const name = window.prompt("新的分组名称", group.name);
    if (name === null || !name.trim() || name.trim() === group.name) return;
    this.api(`/api/groups/${id}`, { method: "PATCH", body: { name: name.trim() } })
      .then(() => this.loadState(false)).then(() => this.toast("分组名称已更新"))
      .catch(error => this.toast(error.message, "error"));
  },

  async deleteGroup(id) {
    const group = this.state.groups.find(item => item.id === id);
    if (!group || !window.confirm(`删除分组“${group.name}”？其中的工作流会移到“未分组”。`)) return;
    try {
      await this.api(`/api/groups/${id}`, { method: "DELETE" });
      await this.loadState(false);
      this.toast("分组已删除，工作流已移到未分组");
    } catch (error) { this.toast(error.message, "error"); }
  },

  async deleteProfile(id) {
    if (!id) return;
    const profile = this.state.profiles.find(item => item.id === id);
    if (!window.confirm(`删除工作流“${profile?.name || id}”？历史任务不会被删除。`)) return;
    try {
      await this.api(`/api/profiles/${id}`, { method: "DELETE" });
      this.state.profiles = this.state.profiles.filter(p => p.id !== id);
      if (this.state.currentProfile?.id === id) this.selectProfile("");
      this.renderProfiles();
      this.toast("工作流已删除；历史任务仍保留");
    } catch (error) { this.toast(error.message, "error"); }
  },

  async toggleQueue() {
    const paused = Boolean(this.state.settings.queue_paused);
    try {
      const data = await this.api(`/api/queue/${paused ? "resume" : "pause"}`, { method: "POST", body: {} });
      this.state.settings = data.settings;
      this.renderQueue();
      this.toast(paused ? "队列已继续" : "队列将在当前任务结束后暂停");
    } catch (error) { this.toast(error.message, "error"); }
  },

  async saveKey(host) {
    const input = document.querySelector(`#${host}KeyInput`);
    const apiKey = input.value.trim();
    if (!apiKey) return this.toast("请输入 API Key", "error");
    try {
      await this.api("/api/key", { method: "POST", body: { host, apiKey } });
      input.value = "";
      await this.loadState(false);
      this.toast(`${this.hostInfo(host).domain} 的密钥已保存`);
    } catch (error) { this.toast(error.message, "error"); }
  },

  async testKey(host) {
    const target = document.querySelector(`#${host}AccountInfo`);
    target.textContent = "正在连接…";
    try {
      const { data } = await this.api("/api/key/test", { method: "POST", body: { host } });
      target.textContent = `连接正常 · 余额 ${data?.remainCoins ?? "—"} 币 · 当前任务 ${data?.currentTaskCounts ?? "—"}`;
      this.toast("连接测试成功");
    } catch (error) { target.textContent = error.message; this.toast(error.message, "error"); }
  },

  async deleteKey(host) {
    try {
      await this.api(`/api/key?host=${encodeURIComponent(host)}`, { method: "DELETE" });
      await this.loadState(false);
      this.toast("密钥已移除");
    } catch (error) { this.toast(error.message, "error"); }
  },

  async saveSettings() {
    try {
      const data = await this.api("/api/settings", {
        method: "POST", body: {
          download_dir: document.querySelector("#downloadDirInput").value,
          poll_interval: Number(document.querySelector("#pollIntervalInput").value),
          timeout_minutes: Number(document.querySelector("#timeoutInput").value),
          default_host: document.querySelector("#hostSelect").value,
          default_workflow_id: document.querySelector("#workflowIdInput").value.trim(),
          auto_download: document.querySelector("#autoDownloadCheck").checked,
          send_full_workflow: document.querySelector("#sendWorkflowCheck").checked,
        },
      });
      this.state.settings = data.settings;
      document.querySelector("#downloadDirInput").value = data.settings.download_dir;
      this.toast("设置已保存");
    } catch (error) { this.toast(error.message, "error"); }
  },

  bind() {
    document.querySelectorAll(".nav-item").forEach(el => el.addEventListener("click", () => this.navigate(el.dataset.view)));
    document.querySelector("#hostSelect").addEventListener("change", () => this.renderConnection());
    document.querySelector("#profileSelect").addEventListener("change", event => this.selectProfile(event.target.value));
    document.querySelector("#newTaskTabButton").addEventListener("click", () => this.createTaskTab());
    document.querySelector("#fetchWorkflowButton").addEventListener("click", () => this.fetchWorkflow());
    document.querySelector("#workflowFileInput").addEventListener("change", event => {
      if (event.target.files[0]) this.importWorkflow(event.target.files[0]);
      event.target.value = "";
    });
    document.querySelector("#deleteProfileButton").addEventListener("click", () => this.deleteProfile(this.state.currentProfile?.id));
    document.querySelector("#toggleAdvancedButton").addEventListener("click", () => { this.state.showAdvanced = !this.state.showAdvanced; this.renderParams(); });
    document.querySelector("#resetParamsButton").addEventListener("click", () => this.resetCurrentParams());
    document.querySelectorAll(".stepper button").forEach(button => button.addEventListener("click", () => {
      const input = document.querySelector("#runsInput");
      input.value = Math.max(1, Math.min(200, Number(input.value || 1) + Number(button.dataset.step)));
    }));
    document.querySelector("#enqueueButton").addEventListener("click", () => this.enqueue());
    document.querySelector("#parameterFileInput").addEventListener("change", event => this.uploadParameterFile(event.target.files[0]));
    document.addEventListener("paste", event => this.handleClipboardPaste(event));
    document.querySelector("#pauseQueueButton").addEventListener("click", () => this.toggleQueue());
    document.querySelector("#queuePauseButton").addEventListener("click", () => this.toggleQueue());
    document.querySelector("#clearJobsButton").addEventListener("click", async () => {
      const finished = (this.state.jobs || []).filter(job => ["SUCCESS", "FAILED", "CANCELLED"].includes(job.status)).length;
      if (!finished) { this.toast("没有已结束的任务需要清理"); return; }
      if (!confirm(`将清理 ${finished} 条已结束的任务记录。\n等待中和执行中的任务会保留，结果图库不受影响。`)) return;
      try {
        const data = await this.api("/api/jobs/finished", { method: "DELETE" });
        this.toast(`已清理 ${data.count} 条记录 · 结果图库保留 ${data.gallery} 个结果`);
        await this.loadState(false);
        this.renderResults(true);
      } catch (error) { this.toast(error.message, "error"); }
    });
    document.querySelector("#openDownloadsButton").addEventListener("click", async () => {
      try { await this.api("/api/open-downloads", { method: "POST", body: {} }); }
      catch (error) { this.toast(error.message, "error"); }
    });
    document.querySelector("#downloadAllVisibleButton").addEventListener("click", async event => {
      const pending = (this.state.gallery || []).filter(item => !item.hasLocal);
      this.setBusy(event.target, true);
      try {
        for (const item of pending) {
          try { await this.api(`/api/gallery/${encodeURIComponent(item.id)}/save`, { method: "POST", body: {} }); }
          catch (error) { this.toast(error.message, "error"); }
        }
        this.toast(pending.length ? `已保存 ${pending.length} 个结果` : "所有结果都已保存在本地");
        await this.loadState(false);
        this.renderResults(true);
      } catch (error) { this.toast(error.message, "error"); }
      finally { this.setBusy(event.target, false); }
    });
    document.querySelector("#rescanGalleryButton").addEventListener("click", async event => {
      this.setBusy(event.target, true, "扫描中…");
      try {
        const data = await this.api("/api/gallery/scan", { method: "POST", body: {} });
        this.state.gallery = data.items || [];
        this.toast(data.count ? `新索引了 ${data.count} 个本地文件` : "没有发现新的本地文件");
        this.renderResults(true);
      } catch (error) { this.toast(error.message, "error"); }
      finally { this.setBusy(event.target, false); }
    });
    document.querySelector("#gallerySearch").addEventListener("input", event => {
      clearTimeout(this.state.gallerySearchTimer);
      this.state.gallerySearchTimer = setTimeout(() => {
        this.state.galleryFilters.query = event.target.value;
        this.renderResults();
      }, 200);
    });
    document.querySelector("#galleryWorkflowFilter").addEventListener("change", event => {
      this.state.galleryFilters.workflow = event.target.value;
      this.renderResults();
    });
    document.querySelector("#galleryTypeFilter").addEventListener("change", event => {
      this.state.galleryFilters.type = event.target.value;
      this.renderResults();
    });
    document.querySelector("#gallerySort").addEventListener("change", event => {
      this.state.galleryFilters.sort = event.target.value;
      this.renderResults();
    });
    document.querySelector("#galleryFilterClear").addEventListener("click", () => {
      this.state.galleryFilters = { query: "", workflow: "", type: "", sort: this.state.galleryFilters.sort };
      document.querySelector("#gallerySearch").value = "";
      document.querySelector("#galleryWorkflowFilter").value = "";
      document.querySelector("#galleryTypeFilter").value = "";
      this.renderResults(true);
    });
    document.querySelector("#saveSettingsButton").addEventListener("click", () => this.saveSettings());
    document.querySelector("#workflowSearchInput").addEventListener("input", () => this.renderSettingsProfiles());
    document.querySelector("#createGroupButton").addEventListener("click", () => this.createGroup());
    document.querySelector("#newGroupInput").addEventListener("keydown", event => { if (event.key === "Enter") this.createGroup(); });
    document.querySelectorAll("[data-action=save-key]").forEach(el => el.addEventListener("click", () => this.saveKey(el.dataset.host)));
    document.querySelectorAll("[data-action=test-key]").forEach(el => el.addEventListener("click", () => this.testKey(el.dataset.host)));
    document.querySelectorAll("[data-action=delete-key]").forEach(el => el.addEventListener("click", () => this.deleteKey(el.dataset.host)));
    document.querySelector("#modalCloseButton").addEventListener("click", () => document.querySelector("#previewModal").classList.add("hidden"));
    document.querySelector("#previewModal").addEventListener("click", event => { if (event.target.id === "previewModal") event.currentTarget.classList.add("hidden"); });
    document.addEventListener("keydown", event => {
      const modal = document.querySelector("#previewModal");
      if (event.key === "Escape") modal.classList.add("hidden");
      if (modal.classList.contains("hidden") || this.state.currentPreviewIndex < 0) return;
      if (["ArrowLeft", "ArrowUp"].includes(event.key)) {
        event.preventDefault(); this.stepGallery(-1);
      }
      if (["ArrowRight", "ArrowDown"].includes(event.key)) {
        event.preventDefault(); this.stepGallery(1);
      }
    });
  },

  async start() {
    this.bind();
    await this.loadState(true);
    setInterval(() => this.loadState(false), 2500);
  },
};

window.addEventListener("resize", () => {
  clearTimeout(window.__galleryLayoutTimer);
  window.__galleryLayoutTimer = setTimeout(() => app.layoutResultGrid(), 150);
});
window.addEventListener("DOMContentLoaded", () => app.start());
