import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Bot,
  Brush,
  Check,
  ChevronDown,
  Clock3,
  Copy,
  Download,
  Edit3,
  Eraser,
  ExternalLink,
  FolderOpen,
  ImagePlus,
  Images,
  KeyRound,
  Loader2,
  MessageCircle,
  PanelLeft,
  RefreshCw,
  Send,
  Settings2,
  Sparkles,
  Wand2,
  X,
} from "lucide-react";
import "./styles.css";

const API = "";

const defaultConfig = {
  base_url: "https://api.xiaoxin.best/",
  api_key: "",
};

const chatModelOptions = [
  { value: "gpt-5.4", label: "GPT-5.4" },
  { value: "gpt-5.5", label: "GPT-5.5" },
];

const imageModelOptions = [
  { value: "gpt-image-2", label: "GPT Image 2" },
  { value: "gpt-image-1", label: "GPT Image 1" },
];

const sizeOptions = [
  { value: "1536x1024", label: "电脑横屏 2K 高清" },
  { value: "1024x1536", label: "手机竖屏 2K 高清" },
  { value: "1024x1024", label: "方图 1K 标准" },
  { value: "auto", label: "自动比例" },
];

const qualityOptions = [
  { value: "high", label: "2K 高清" },
  { value: "medium", label: "1K 标准" },
  { value: "low", label: "快速预览" },
  { value: "auto", label: "自动清晰度" },
];

const backgroundOptions = [
  { value: "auto", label: "自动背景" },
  { value: "transparent", label: "透明背景" },
  { value: "opaque", label: "不透明背景" },
];

const formatOptions = [
  { value: "png", label: "PNG" },
  { value: "jpeg", label: "JPEG" },
  { value: "webp", label: "WebP" },
];

const actionOptions = [
  { value: "auto", label: "自动判断" },
  { value: "generate", label: "生成新图" },
  { value: "edit", label: "编辑参考图" },
];

const fidelityOptions = [
  { value: "auto", label: "自动保真" },
  { value: "high", label: "高保真" },
  { value: "low", label: "低保真" },
];

const moderationOptions = [
  { value: "auto", label: "自动审核" },
  { value: "low", label: "低审核" },
];

const defaults = {
  mode: "chat",
  prompt: "",
  model: "gpt-5.4",
  chatModel: "gpt-5.4",
  imageModel: "gpt-image-2",
  action: "auto",
  size: "1536x1024",
  quality: "high",
  n: 1,
  background: "auto",
  output_format: "png",
  output_compression: "",
  moderation: "auto",
  input_fidelity: "auto",
  partial_images: 0,
  context_limit: 10,
};

function readJsonStorage(key, fallback) {
  try {
    const saved = localStorage.getItem(key);
    return saved ? { ...fallback, ...JSON.parse(saved) } : fallback;
  } catch {
    return fallback;
  }
}

function persistableForm(form) {
  const { prompt, ...settings } = form;
  return settings;
}

function normalizeFormSettings(settings) {
  const next = { ...settings };
  if (!chatModelOptions.some((option) => option.value === next.chatModel)) {
    next.chatModel = defaults.chatModel;
  }
  if (!imageModelOptions.some((option) => option.value === next.imageModel)) {
    next.imageModel = defaults.imageModel;
  }
  if (!chatModelOptions.some((option) => option.value === next.model)) {
    next.model = defaults.model;
  }
  if (!sizeOptions.some((option) => option.value === next.size)) {
    next.size = defaults.size;
  }
  if (!qualityOptions.some((option) => option.value === next.quality)) {
    next.quality = defaults.quality;
  }
  return next;
}

function optionLabel(options, value) {
  return options.find((option) => option.value === value)?.label || value;
}

function App() {
  const [config, setConfig] = useState(() => readJsonStorage("gpt-image-config", defaultConfig));
  const [form, setForm] = useState(() => ({
    ...normalizeFormSettings(readJsonStorage("gpt-image-form-settings", defaults)),
    prompt: "",
  }));
  const [controlsOpen, setControlsOpen] = useState(() => readJsonStorage("gpt-image-controls", { open: false }).open);
  const [openGroups, setOpenGroups] = useState(() => readJsonStorage("gpt-image-open-groups", {}));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [gallery, setGallery] = useState([]);
  const [galleryHistory, setGalleryHistory] = useState([]);
  const [conversations, setConversations] = useState([]);
  const [selectedHistory, setSelectedHistory] = useState(null);
  const [activeView, setActiveView] = useState("studio");
  const [conversation, setConversation] = useState(null);
  const [messages, setMessages] = useState([]);
  const [editImages, setEditImages] = useState([]);
  const [editMask, setEditMask] = useState(null);
  const [chatImages, setChatImages] = useState([]);
  const [copied, setCopied] = useState("");
  const scrollRef = useRef(null);

  useEffect(() => {
    localStorage.setItem("gpt-image-config", JSON.stringify(config));
  }, [config]);

  useEffect(() => {
    localStorage.setItem("gpt-image-form-settings", JSON.stringify(persistableForm(form)));
  }, [form]);

  useEffect(() => {
    localStorage.setItem("gpt-image-open-groups", JSON.stringify(openGroups));
  }, [openGroups]);

  useEffect(() => {
    localStorage.setItem("gpt-image-controls", JSON.stringify({ open: controlsOpen }));
  }, [controlsOpen]);

  useEffect(() => {
    fetch(`${API}/api/settings`)
      .then((res) => res.json())
      .then((data) => {
        setConfig((current) => ({
          base_url: current.base_url || data.base_url || defaultConfig.base_url,
          api_key: current.api_key || data.api_key || "",
        }));
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshHistory();
    refreshGallery();
  }, []);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading]);

  const submitDisabled = loading || !form.prompt.trim();

  async function ensureConversation() {
    if (conversation) return conversation;
    const res = await fetch(`${API}/api/conversations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: form.prompt.slice(0, 24) || "新的生图对话", context_limit: Number(form.context_limit) }),
    });
    const data = await parse(res);
    setConversation(data);
    refreshHistory();
    return data;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (form.mode === "generate") {
        await runGenerate();
      } else if (form.mode === "edit") {
        await runEdit();
      } else {
        await runChat();
      }
      setForm((value) => ({ ...value, prompt: "" }));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setLoading(false);
    }
  }

  async function runGenerate() {
    const body = {
      prompt: form.prompt,
      model: form.model,
      image_model: form.imageModel,
      size: form.size,
      quality: form.quality,
      n: Number(form.n),
      background: form.background,
      output_format: form.output_format,
      output_compression: form.output_compression === "" ? null : Number(form.output_compression),
      moderation: form.moderation,
      action: "generate",
      partial_images: Number(form.partial_images),
      config,
    };
    const res = await fetch(`${API}/api/images/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await parse(res);
    setGallery((items) => [{ prompt: form.prompt, mode: "generate", images: data.images }, ...items]);
    refreshGallery();
  }

  async function runEdit() {
    if (!editImages.length) {
      throw new Error("编辑模式至少上传一张图片");
    }
    const data = new FormData();
    const params = {
      prompt: form.prompt,
      model: form.model,
      image_model: form.imageModel,
      size: form.size,
      quality: form.quality,
      n: Number(form.n),
      background: form.background,
      output_format: form.output_format,
      output_compression: form.output_compression === "" ? null : Number(form.output_compression),
      moderation: form.moderation,
      input_fidelity: form.input_fidelity,
      partial_images: Number(form.partial_images),
      config,
    };
    data.append("params_json", JSON.stringify(params));
    [...editImages].forEach((file) => data.append("images", file));
    if (editMask) data.append("mask", editMask);
    const res = await fetch(`${API}/api/images/edit`, { method: "POST", body: data });
    const result = await parse(res);
    setGallery((items) => [{ prompt: form.prompt, mode: "edit", images: result.images }, ...items]);
    refreshGallery();
  }

  async function runChat() {
    const active = await ensureConversation();
    const localUser = {
      id: `u-${Date.now()}`,
      role: "user",
      content: form.prompt,
      previews: [...chatImages].map((file) => URL.createObjectURL(file)),
    };
    setMessages((items) => [...items, localUser]);

    const data = new FormData();
    const params = {
      prompt: form.prompt,
      model: form.chatModel,
      image_model: form.imageModel,
      action: form.action,
      size: form.size,
      quality: form.quality,
      background: form.background,
      output_format: form.output_format,
      output_compression: form.output_compression === "" ? null : Number(form.output_compression),
      moderation: form.moderation,
      input_fidelity: form.input_fidelity,
      partial_images: Number(form.partial_images),
      context_limit: Number(form.context_limit),
      config,
    };
    data.append("params_json", JSON.stringify(params));
    [...chatImages].forEach((file) => data.append("images", file));
    const res = await fetch(`${API}/api/conversations/${active.id}/messages`, {
      method: "POST",
      body: data,
    });
    const result = await parse(res);
    setMessages((items) => [
      ...items.map((item) => (item.id === localUser.id ? { ...item, id: result.user_message_id } : item)),
      {
        id: result.assistant_message_id,
        role: "assistant",
        content: result.text || "已生成图片。",
        images: result.images,
      },
    ]);
    setChatImages([]);
    refreshHistory();
    refreshGallery();
  }

  async function saveSettings() {
    const res = await fetch(`${API}/api/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    await parse(res);
    setCopied("配置已保存");
    setTimeout(() => setCopied(""), 1400);
  }

  async function newChat() {
    setConversation(null);
    setMessages([]);
    setChatImages([]);
    setActiveView("studio");
  }

  async function refreshHistory() {
    try {
      const res = await fetch(`${API}/api/conversations`);
      const data = await parse(res);
      setConversations(data.items || []);
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function refreshGallery() {
    try {
      const res = await fetch(`${API}/api/gallery`);
      const data = await parse(res);
      setGalleryHistory(data.items || []);
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function loadConversation(id, { openStudio = true } = {}) {
    const res = await fetch(`${API}/api/conversations/${id}`);
    const data = await parse(res);
    const imagesByMessage = new Map();
    for (const image of data.images || []) {
      const list = imagesByMessage.get(image.message_id) || [];
      list.push({
        id: image.id,
        url: image.public_url,
        mime_type: image.mime_type,
        conversation_id: image.conversation_id,
        message_id: image.message_id,
        file_path: image.file_path,
        filename: image.file_path?.split(/[\\/]/).pop() || "generated-image.png",
        title: image.title,
        bucket: image.bucket,
      });
      imagesByMessage.set(image.message_id, list);
    }
    const hydrated = (data.messages || []).map((msg) => ({ ...msg, images: imagesByMessage.get(msg.id) || [] }));
    setConversation(data.conversation);
    setMessages(hydrated);
    setForm((value) => ({ ...value, mode: "chat", context_limit: data.conversation.context_limit ?? value.context_limit }));
    setSelectedHistory({ ...data, messages: hydrated });
    if (openStudio) setActiveView("studio");
  }

  async function saveConversationMeta(next) {
    if (!selectedHistory?.conversation) return;
    const res = await fetch(`${API}/api/conversations/${selectedHistory.conversation.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(next),
    });
    const updated = await parse(res);
    setSelectedHistory((current) => ({ ...current, conversation: updated }));
    if (conversation?.id === updated.id) setConversation(updated);
    refreshHistory();
  }

  async function saveMessage(messageId, content) {
    const res = await fetch(`${API}/api/messages/${messageId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    const updated = await parse(res);
    setSelectedHistory((current) => ({
      ...current,
      messages: current.messages.map((msg) => (msg.id === messageId ? { ...msg, ...updated } : msg)),
    }));
    if (conversation?.id === updated.conversation_id) {
      setMessages((items) => items.map((msg) => (msg.id === messageId ? { ...msg, ...updated } : msg)));
    }
  }

  async function useImageAsReference(image) {
    const response = await fetch(image.public_url || image.url);
    const blob = await response.blob();
    const filename = image.file_path?.split(/[\\/]/).pop() || image.filename || "history-image.png";
    const file = new File([blob], filename, { type: image.mime_type || blob.type || "image/png" });
    if (image.conversation_id) {
      await loadConversation(image.conversation_id, { openStudio: true });
    } else {
      setActiveView("studio");
      setForm((value) => ({ ...value, mode: "chat" }));
    }
    setChatImages([file]);
    setForm((value) => ({ ...value, prompt: "", mode: "chat", action: "edit" }));
  }

  async function downloadImage(image) {
    const response = await fetch(image.public_url || image.url);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = image.filename || image.file_path?.split(/[\\/]/).pop() || "generated-image.png";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function toggleGroup(name) {
    setOpenGroups((groups) => ({ ...groups, [name]: !groups[name] }));
  }

  const modeMeta = useMemo(() => {
    if (form.mode === "generate") return { icon: Wand2, title: "普通生图" };
    if (form.mode === "edit") return { icon: Eraser, title: "图片编辑" };
    return { icon: MessageCircle, title: "对话生图" };
  }, [form.mode]);
  const ModeIcon = modeMeta.icon;

  return (
    <main className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brandMark"><Sparkles size={22} /></div>
          <div>
            <h1>GPT Image Studio</h1>
            <span>个人生图工作台</span>
          </div>
        </div>
        <button className={`iconButton ${controlsOpen ? "active" : ""}`} onClick={() => setControlsOpen((v) => !v)} title="配置">
          <PanelLeft size={20} />
        </button>
      </header>

      <section className={`workspace ${controlsOpen ? "withControls" : "withoutControls"}`}>
        {controlsOpen && (
        <aside className="controls">
          <div className="modeSwitch">
            {[
              ["chat", MessageCircle, "对话"],
              ["generate", Wand2, "生成"],
              ["edit", Brush, "编辑"],
            ].map(([value, Icon, label]) => (
              <button
                key={value}
                className={form.mode === value ? "active" : ""}
                onClick={() => setForm((f) => ({ ...f, mode: value }))}
              >
                <Icon size={17} />
                {label}
              </button>
            ))}
          </div>

          <SettingsGroup
            title="接口配置"
            summary={config.base_url || "未配置"}
            open={!!openGroups.endpoint}
            onToggle={() => toggleGroup("endpoint")}
          >
            <Field label="接口地址">
              <input value={config.base_url} onChange={(e) => setConfig({ ...config, base_url: e.target.value })} />
            </Field>
            <Field label="密钥">
              <input
                type="password"
                value={config.api_key}
                onChange={(e) => setConfig({ ...config, api_key: e.target.value })}
                placeholder="sk-..."
              />
            </Field>
            <button className="secondaryButton" onClick={saveSettings}>
              {copied ? <Check size={17} /> : <KeyRound size={17} />}
              {copied || "保存配置"}
            </button>
          </SettingsGroup>

          <SettingsGroup
            title="模型设置"
            summary={form.mode === "chat" ? `${form.chatModel} / ${form.imageModel}` : `${form.model} / ${form.imageModel}`}
            open={!!openGroups.models}
            onToggle={() => toggleGroup("models")}
          >
            {form.mode === "chat" ? (
              <>
                <Select label="对话模型" value={form.chatModel} onChange={(v) => setForm({ ...form, chatModel: v })} options={chatModelOptions} />
                <Select label="图片工具模型" value={form.imageModel} onChange={(v) => setForm({ ...form, imageModel: v })} options={imageModelOptions} />
              </>
            ) : (
              <>
                <Select label="Responses 模型" value={form.model} onChange={(v) => setForm({ ...form, model: v })} options={chatModelOptions} />
                <Select label="图片工具模型" value={form.imageModel} onChange={(v) => setForm({ ...form, imageModel: v })} options={imageModelOptions} />
              </>
            )}
          </SettingsGroup>

          <SettingsGroup
            title="图片参数"
            summary={`${optionLabel(sizeOptions, form.size)} / ${optionLabel(qualityOptions, form.quality)} / ${optionLabel(formatOptions, form.output_format)}`}
            open={!!openGroups.image}
            onToggle={() => toggleGroup("image")}
          >
            <Select label="画面比例" value={form.size} onChange={(v) => setForm({ ...form, size: v })} options={sizeOptions} />
            <Select label="分辨率" value={form.quality} onChange={(v) => setForm({ ...form, quality: v })} options={qualityOptions} />
            <Select label="背景" value={form.background} onChange={(v) => setForm({ ...form, background: v })} options={backgroundOptions} />
            <Select label="格式" value={form.output_format} onChange={(v) => setForm({ ...form, output_format: v })} options={formatOptions} />
            {form.mode !== "chat" && (
              <Field label="数量">
                <input type="number" min="1" max="10" value={form.n} onChange={(e) => setForm({ ...form, n: e.target.value })} />
              </Field>
            )}
          </SettingsGroup>

          <SettingsGroup
            title="高级选项"
            summary={form.mode === "chat" ? `${optionLabel(actionOptions, form.action)} / ${optionLabel(fidelityOptions, form.input_fidelity)}` : optionLabel(moderationOptions, form.moderation)}
            open={!!openGroups.advanced}
            onToggle={() => toggleGroup("advanced")}
          >
            {form.mode === "chat" ? (
              <>
                <Select label="动作" value={form.action} onChange={(v) => setForm({ ...form, action: v })} options={actionOptions} />
                <Select label="输入保真" value={form.input_fidelity} onChange={(v) => setForm({ ...form, input_fidelity: v })} options={fidelityOptions} />
                <Select label="局部图" value={String(form.partial_images)} onChange={(v) => setForm({ ...form, partial_images: Number(v) })} options={["0", "1", "2", "3"]} />
                <Field label="上下文条数">
                  <input type="number" min="0" max="50" value={form.context_limit} onChange={(e) => setForm({ ...form, context_limit: e.target.value })} />
                </Field>
                <Field label="压缩 0-100">
                  <input value={form.output_compression} onChange={(e) => setForm({ ...form, output_compression: e.target.value })} placeholder="可留空" />
                </Field>
                <Select label="审核" value={form.moderation} onChange={(v) => setForm({ ...form, moderation: v })} options={moderationOptions} />
              </>
            ) : form.mode === "edit" ? (
              <>
                <Select label="输入保真" value={form.input_fidelity} onChange={(v) => setForm({ ...form, input_fidelity: v })} options={fidelityOptions} />
                <Select label="局部图" value={String(form.partial_images)} onChange={(v) => setForm({ ...form, partial_images: Number(v) })} options={["0", "1", "2", "3"]} />
                <Field label="压缩 0-100">
                  <input value={form.output_compression} onChange={(e) => setForm({ ...form, output_compression: e.target.value })} placeholder="可留空" />
                </Field>
                <Select label="审核" value={form.moderation} onChange={(v) => setForm({ ...form, moderation: v })} options={moderationOptions} />
              </>
            ) : (
              <>
                <Field label="压缩 0-100">
                  <input value={form.output_compression} onChange={(e) => setForm({ ...form, output_compression: e.target.value })} placeholder="可留空" />
                </Field>
                <Select label="审核" value={form.moderation} onChange={(v) => setForm({ ...form, moderation: v })} options={moderationOptions} />
              </>
            )}
          </SettingsGroup>
        </aside>
        )}

        <section className="stage">
          <nav className="viewTabs">
            {[
              ["studio", Sparkles, "工作台"],
              ["history", Clock3, "历史"],
              ["gallery", Images, "图库"],
            ].map(([value, Icon, label]) => (
              <button key={value} className={activeView === value ? "active" : ""} onClick={() => setActiveView(value)}>
                <Icon size={16} />
                {label}
              </button>
            ))}
          </nav>
          <div className="stageHead">
            <div>
              <p><ModeIcon size={18} /> {modeMeta.title}</p>
              <h2>{activeView === "history" ? "对话历史可查看和修改" : activeView === "gallery" ? "历史图片按对话和时间保存" : form.mode === "chat" ? "像聊天一样连续生图" : "提交后生成图片到图库"}</h2>
            </div>
            {activeView === "studio" && form.mode === "chat" && (
              <button className="ghostButton" onClick={newChat}><RefreshCw size={17} /> 新对话</button>
            )}
          </div>

          {error && <ErrorPanel error={error} onClose={() => setError(null)} />}

          {activeView === "history" ? (
            <HistoryPane
              conversations={conversations}
              selected={selectedHistory}
              onRefresh={refreshHistory}
              onOpen={(id) => loadConversation(id, { openStudio: false })}
              onContinue={(id) => loadConversation(id, { openStudio: true })}
              onSaveMeta={saveConversationMeta}
              onSaveMessage={saveMessage}
              onDownload={downloadImage}
              onUseImage={useImageAsReference}
            />
          ) : activeView === "gallery" ? (
            <GalleryHistory items={galleryHistory} onRefresh={refreshGallery} onDownload={downloadImage} onUseImage={useImageAsReference} />
          ) : form.mode === "chat" ? (
            <div className="chatPane" ref={scrollRef}>
              {messages.length === 0 && (
                <div className="emptyState">
                  <Bot size={34} />
                  <h3>把想法直接说出来</h3>
                  <p>可以先生成，再上传上一张图继续改，动作选择 auto 时会自动判断。</p>
                </div>
              )}
              {messages.map((msg) => (
                <Message key={msg.id} msg={msg} onDownload={downloadImage} />
              ))}
              {loading && (
                <div className="message assistant">
                  <div className="avatar"><Loader2 className="spin" size={18} /></div>
                  <div className="bubble">正在向生图接口请求，复杂图片可能需要几十秒...</div>
                </div>
              )}
            </div>
          ) : (
            <Gallery items={gallery} loading={loading} onDownload={downloadImage} />
          )}

          {activeView === "studio" && <form className="composer" onSubmit={handleSubmit}>
            {form.mode === "edit" && (
              <UploadRow
                label="编辑图片"
                files={editImages}
                onChange={setEditImages}
                multiple
              />
            )}
            {form.mode === "edit" && (
              <UploadRow
                label="Mask"
                files={editMask ? [editMask] : []}
                onChange={(files) => setEditMask(files[0] || null)}
              />
            )}
            {form.mode === "chat" && (
              <UploadRow
                label="参考图片"
                files={chatImages}
                onChange={setChatImages}
                multiple
              />
            )}
            <div className="promptRow">
              <textarea
                value={form.prompt}
                onChange={(e) => setForm({ ...form, prompt: e.target.value })}
                placeholder={form.mode === "edit" ? "描述你想怎么改这张图..." : "描述你想生成的画面..."}
              />
              <button className="sendButton" disabled={submitDisabled}>
                {loading ? <Loader2 className="spin" size={20} /> : <Send size={20} />}
              </button>
            </div>
          </form>}
        </section>
        <aside className="sideInfo">
          <div className="infoBlock">
            <Settings2 size={18} />
            <h3>参数提示</h3>
            <p>对话模式走 Responses API，普通生成和编辑走 Images API。中转服务只要兼容 OpenAI 路径即可使用。</p>
          </div>
          <div className="quickPrompts">
            {[
              "一只玻璃质感的未来耳机，电商白底产品图，高级摄影",
              "把参考图改成赛博朋克夜景，保持主体轮廓和构图",
              "连续分镜第一帧：少女打开一扇发光的门，油画厚涂，电影感",
            ].map((text) => (
              <button key={text} onClick={() => setForm((f) => ({ ...f, prompt: text }))}>
                <Copy size={15} />
                {text}
              </button>
            ))}
          </div>
        </aside>
      </section>
    </main>
  );
}

function SettingsGroup({ title, summary, open, onToggle, children }) {
  return (
    <section className={`settingsGroup ${open ? "open" : ""}`}>
      <button type="button" className="settingsGroupHead" onClick={onToggle}>
        <span>
          <strong>{title}</strong>
          <small>{summary}</small>
        </span>
        <ChevronDown size={18} />
      </button>
      {open && <div className="settingsGroupBody">{children}</div>}
    </section>
  );
}

function ErrorPanel({ error, onClose }) {
  const [copied, setCopied] = useState(false);
  const detailText = error.detail || error.raw || error.summary;

  async function copyError() {
    await navigator.clipboard.writeText(detailText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  }

  return (
    <section className="errorPanel">
      <div className="errorPanelHead">
        <div>
          <strong>生图失败</strong>
          <span>{error.summary}</span>
        </div>
        <div className="errorActions">
          <button type="button" onClick={copyError}><Copy size={15} /> {copied ? "已复制" : "复制原因"}</button>
          <button type="button" onClick={onClose} title="关闭"><X size={15} /></button>
        </div>
      </div>
      {error.meta?.length > 0 && (
        <div className="errorMeta">
          {error.meta.map(([label, value]) => <span key={label}>{label}: {value}</span>)}
        </div>
      )}
      <pre>{detailText}</pre>
    </section>
  );
}

function HistoryPane({ conversations, selected, onRefresh, onOpen, onContinue, onSaveMeta, onSaveMessage, onDownload, onUseImage }) {
  const [draftTitle, setDraftTitle] = useState("");
  const [draftLimit, setDraftLimit] = useState(10);
  const [messageDrafts, setMessageDrafts] = useState({});

  useEffect(() => {
    if (!selected?.conversation) return;
    setDraftTitle(selected.conversation.title || "");
    setDraftLimit(selected.conversation.context_limit ?? 10);
    setMessageDrafts(Object.fromEntries((selected.messages || []).map((msg) => [msg.id, msg.content])));
  }, [selected]);

  return (
    <div className="historyPane">
      <div className="historyList">
        <div className="paneToolbar">
          <strong>对话</strong>
          <button type="button" onClick={onRefresh}><RefreshCw size={15} />刷新</button>
        </div>
        {conversations.length === 0 ? (
          <div className="emptyMini">暂无历史对话</div>
        ) : conversations.map((item) => (
          <button key={item.id} className={`historyItem ${selected?.conversation?.id === item.id ? "active" : ""}`} onClick={() => onOpen(item.id)}>
            <span>{item.title}</span>
            <small>{item.message_count || 0} 条消息 / {item.image_count || 0} 张图</small>
          </button>
        ))}
      </div>
      <div className="historyDetail">
        {!selected ? (
          <div className="emptyState">
            <FolderOpen size={34} />
            <h3>选择一段历史</h3>
            <p>打开后可以修改标题、上下文条数和每条消息，再继续对话生图。</p>
          </div>
        ) : (
          <>
            <div className="historyMeta">
              <Field label="对话标题">
                <input value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)} />
              </Field>
              <Field label="上下文条数">
                <input type="number" min="0" max="50" value={draftLimit} onChange={(e) => setDraftLimit(e.target.value)} />
              </Field>
              <button className="secondaryButton" type="button" onClick={() => onSaveMeta({ title: draftTitle, context_limit: Number(draftLimit) })}>
                <Check size={16} /> 保存历史设置
              </button>
              <button className="ghostButton" type="button" onClick={() => onContinue(selected.conversation.id)}>
                <MessageCircle size={16} /> 继续对话
              </button>
            </div>
            <div className="historyMessages">
              {(selected.messages || []).map((msg) => (
                <article className="historyMessage" key={msg.id}>
                  <div className="messageRole">{msg.role === "user" ? "用户" : "助手"}</div>
                  <textarea value={messageDrafts[msg.id] ?? msg.content} onChange={(e) => setMessageDrafts((drafts) => ({ ...drafts, [msg.id]: e.target.value }))} />
                  <div className="historyMessageActions">
                    <button type="button" onClick={() => onSaveMessage(msg.id, messageDrafts[msg.id] ?? msg.content)}>
                      <Edit3 size={15} /> 保存修改
                    </button>
                  </div>
                  {msg.images?.length > 0 && (
                    <div className="imageGrid">
                      {msg.images.map((image) => (
                        <ImageCard key={image.url} image={image} onDownload={onDownload} onUseImage={onUseImage} />
                      ))}
                    </div>
                  )}
                </article>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function GalleryHistory({ items, onRefresh, onDownload, onUseImage }) {
  const groups = groupImages(items);
  return (
    <div className="galleryHistory">
      <div className="paneToolbar">
        <strong>历史图库</strong>
        <button type="button" onClick={onRefresh}><RefreshCw size={15} />刷新</button>
      </div>
      {groups.length === 0 ? (
        <div className="emptyState">
          <Images size={34} />
          <h3>还没有历史图片</h3>
          <p>之后生成的图片会按对话标题和时间保存到这里。</p>
        </div>
      ) : groups.map((group) => (
        <article className="galleryGroup" key={group.key}>
          <div>
            <span>{group.title}</span>
            <small>{group.time}</small>
          </div>
          <div className="imageGrid">
            {group.items.map((image) => <ImageCard key={image.id} image={image} onDownload={onDownload} onUseImage={onUseImage} />)}
          </div>
        </article>
      ))}
    </div>
  );
}

function Message({ msg, onDownload }) {
  return (
    <div className={`message ${msg.role}`}>
      <div className="avatar">{msg.role === "user" ? "你" : <Bot size={18} />}</div>
      <div className="bubble">
        <p>{msg.content}</p>
        {msg.previews?.length > 0 && (
          <div className="imageGrid">
            {msg.previews.map((url) => <img key={url} src={url} alt="" />)}
          </div>
        )}
        {msg.images?.length > 0 && (
          <div className="imageGrid">
            {msg.images.map((image) => <ImageCard key={image.url} image={image} onDownload={onDownload} />)}
          </div>
        )}
      </div>
    </div>
  );
}

function Gallery({ items, loading, onDownload }) {
  return (
    <div className="galleryPane">
      {items.length === 0 && !loading && (
        <div className="emptyState">
          <ImagePlus size={34} />
          <h3>生成结果会出现在这里</h3>
          <p>普通生成可一次生成多张，编辑模式支持参考图和透明 mask。</p>
        </div>
      )}
      {loading && (
        <div className="emptyState">
          <Loader2 className="spin" size={34} />
          <h3>正在生成</h3>
          <p>请保持页面打开，完成后图片会自动保存到本地 storage。</p>
        </div>
      )}
      {items.map((item, index) => (
        <article className="resultGroup" key={`${item.prompt}-${index}`}>
          <span>{item.mode}</span>
          <h3>{item.prompt}</h3>
          <div className="imageGrid">
            {item.images.map((image) => <ImageCard key={image.url} image={image} onDownload={onDownload} />)}
          </div>
        </article>
      ))}
    </div>
  );
}

function ImageCard({ image, onDownload, onUseImage }) {
  const url = image.public_url || image.url;
  return (
    <div className="imageCard">
      <img src={url} alt="generated" />
      <div className="imageActions">
        <a href={url} target="_blank" rel="noreferrer">
          <ExternalLink size={14} />
          预览
        </a>
        <button type="button" onClick={() => onDownload(image)}>
          <Download size={14} />
          下载
        </button>
        {onUseImage && (
          <button type="button" onClick={() => onUseImage(image)}>
            <Brush size={14} />
            继续改
          </button>
        )}
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function Select({ label, value, onChange, options }) {
  return (
    <Field label={label}>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => {
          const normalized = typeof option === "string" ? { value: option, label: option } : option;
          return <option key={normalized.value} value={normalized.value}>{normalized.label}</option>;
        })}
      </select>
    </Field>
  );
}

function UploadRow({ label, files, onChange, multiple = false }) {
  return (
    <label className="uploadRow">
      <span><ImagePlus size={16} /> {label}</span>
      <input
        type="file"
        accept="image/*"
        multiple={multiple}
        onChange={(event) => onChange([...event.target.files])}
      />
      <small>{files.length ? files.map((file) => file.name).join("，") : "未选择"}</small>
    </label>
  );
}

async function parse(res) {
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: { raw: text } };
  }
  if (!res.ok) throw data;
  return data;
}

function describeError(err) {
  if (err instanceof Error) {
    return { summary: err.message, detail: err.stack || err.message, meta: [] };
  }
  if (typeof err === "string") {
    return { summary: err, detail: err, meta: [] };
  }
  const detail = err?.detail ?? err;
  const status = detail?.status_code || err?.status || "";
  const endpoint = detail?.endpoint || detail?.fallback_error?.endpoint || "";
  const url = detail?.url || detail?.fallback_error?.url || "";
  const upstream = detail?.upstream || detail?.fallback_error?.upstream || detail;
  const summary = detail?.message || upstream?.message || upstream?.raw || "请求失败";
  const meta = [
    status ? ["状态码", status] : null,
    endpoint ? ["接口", endpoint] : null,
    url ? ["地址", url] : null,
  ].filter(Boolean);
  return {
    summary,
    meta,
    detail: JSON.stringify(detail, null, 2),
    raw: JSON.stringify(err, null, 2),
  };
}

function groupImages(items) {
  const map = new Map();
  for (const item of items || []) {
    const title = item.conversation_title || item.title || item.task_prompt || "独立生成";
    const bucket = item.bucket || item.created_at || "default";
    const key = `${title}-${bucket}`;
    if (!map.has(key)) {
      map.set(key, {
        key,
        title,
        time: item.created_at ? new Date(item.created_at).toLocaleString() : bucket,
        items: [],
      });
    }
    map.get(key).items.push(item);
  }
  return [...map.values()];
}

createRoot(document.getElementById("root")).render(<App />);
