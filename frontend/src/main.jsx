import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Bot,
  Brush,
  Check,
  ChevronDown,
  Copy,
  Download,
  Eraser,
  ExternalLink,
  ImagePlus,
  KeyRound,
  Loader2,
  MessageCircle,
  PanelRight,
  RefreshCw,
  Send,
  Settings2,
  Sparkles,
  Wand2,
} from "lucide-react";
import "./styles.css";

const API = "";

const defaultConfig = {
  base_url: "https://api.xiaoxin.best/",
  api_key: "",
};

const defaults = {
  mode: "chat",
  prompt: "",
  model: "gpt-image-1",
  chatModel: "gpt-4.1-mini",
  imageModel: "gpt-image-1",
  action: "auto",
  size: "1024x1024",
  quality: "auto",
  n: 1,
  background: "auto",
  output_format: "png",
  output_compression: "",
  moderation: "auto",
  input_fidelity: "auto",
  partial_images: 0,
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

function App() {
  const [config, setConfig] = useState(() => readJsonStorage("gpt-image-config", defaultConfig));
  const [form, setForm] = useState(() => ({
    ...readJsonStorage("gpt-image-form-settings", defaults),
    prompt: "",
  }));
  const [settingsOpen, setSettingsOpen] = useState(true);
  const [openGroups, setOpenGroups] = useState(() => readJsonStorage("gpt-image-open-groups", {}));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [gallery, setGallery] = useState([]);
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
      body: JSON.stringify({ title: form.prompt.slice(0, 24) || "新的生图对话" }),
    });
    const data = await parse(res);
    setConversation(data);
    return data;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
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
      setError(formatError(err));
    } finally {
      setLoading(false);
    }
  }

  async function runGenerate() {
    const body = {
      prompt: form.prompt,
      model: form.model,
      size: form.size,
      quality: form.quality,
      n: Number(form.n),
      background: form.background,
      output_format: form.output_format,
      output_compression: form.output_compression === "" ? null : Number(form.output_compression),
      moderation: form.moderation,
      config,
    };
    const res = await fetch(`${API}/api/images/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await parse(res);
    setGallery((items) => [{ prompt: form.prompt, mode: "generate", images: data.images }, ...items]);
  }

  async function runEdit() {
    if (!editImages.length) {
      throw new Error("编辑模式至少上传一张图片");
    }
    const data = new FormData();
    const params = {
      prompt: form.prompt,
      model: form.model,
      size: form.size,
      quality: form.quality,
      n: Number(form.n),
      background: form.background,
      output_format: form.output_format,
      output_compression: form.output_compression === "" ? null : Number(form.output_compression),
      moderation: form.moderation,
      config,
    };
    data.append("params_json", JSON.stringify(params));
    [...editImages].forEach((file) => data.append("images", file));
    if (editMask) data.append("mask", editMask);
    const res = await fetch(`${API}/api/images/edit`, { method: "POST", body: data });
    const result = await parse(res);
    setGallery((items) => [{ prompt: form.prompt, mode: "edit", images: result.images }, ...items]);
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
      input_fidelity: form.input_fidelity,
      partial_images: Number(form.partial_images),
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
      ...items,
      {
        id: result.assistant_message_id,
        role: "assistant",
        content: result.text || "已生成图片。",
        images: result.images,
      },
    ]);
    setChatImages([]);
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
  }

  async function downloadImage(image) {
    const response = await fetch(image.url);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = image.filename || "generated-image.png";
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
        <button className="iconButton" onClick={() => setSettingsOpen((v) => !v)} title="配置">
          <PanelRight size={20} />
        </button>
      </header>

      <section className="workspace">
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
            summary={form.mode === "chat" ? `${form.chatModel} / ${form.imageModel}` : form.model}
            open={!!openGroups.models}
            onToggle={() => toggleGroup("models")}
          >
            {form.mode === "chat" ? (
              <>
                <Field label="对话模型">
                  <input value={form.chatModel} onChange={(e) => setForm({ ...form, chatModel: e.target.value })} />
                </Field>
                <Field label="生图模型">
                  <input value={form.imageModel} onChange={(e) => setForm({ ...form, imageModel: e.target.value })} />
                </Field>
              </>
            ) : (
              <Field label="模型">
                <input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} />
              </Field>
            )}
          </SettingsGroup>

          <SettingsGroup
            title="图片参数"
            summary={`${form.size} / ${form.quality} / ${form.output_format}`}
            open={!!openGroups.image}
            onToggle={() => toggleGroup("image")}
          >
            <Select label="尺寸" value={form.size} onChange={(v) => setForm({ ...form, size: v })} options={["1024x1024", "1024x1536", "1536x1024", "auto"]} />
            <Select label="质量" value={form.quality} onChange={(v) => setForm({ ...form, quality: v })} options={["auto", "low", "medium", "high"]} />
            <Select label="背景" value={form.background} onChange={(v) => setForm({ ...form, background: v })} options={["auto", "transparent", "opaque"]} />
            <Select label="格式" value={form.output_format} onChange={(v) => setForm({ ...form, output_format: v })} options={["png", "jpeg", "webp"]} />
            {form.mode !== "chat" && (
              <Field label="数量">
                <input type="number" min="1" max="10" value={form.n} onChange={(e) => setForm({ ...form, n: e.target.value })} />
              </Field>
            )}
          </SettingsGroup>

          <SettingsGroup
            title="高级选项"
            summary={form.mode === "chat" ? `${form.action} / fidelity ${form.input_fidelity}` : `moderation ${form.moderation}`}
            open={!!openGroups.advanced}
            onToggle={() => toggleGroup("advanced")}
          >
            {form.mode === "chat" ? (
              <>
                <Select label="动作" value={form.action} onChange={(v) => setForm({ ...form, action: v })} options={["auto", "generate", "edit"]} />
                <Select label="输入保真" value={form.input_fidelity} onChange={(v) => setForm({ ...form, input_fidelity: v })} options={["auto", "high", "low"]} />
                <Select label="局部图" value={String(form.partial_images)} onChange={(v) => setForm({ ...form, partial_images: Number(v) })} options={["0", "1", "2", "3"]} />
              </>
            ) : (
              <>
                <Field label="压缩 0-100">
                  <input value={form.output_compression} onChange={(e) => setForm({ ...form, output_compression: e.target.value })} placeholder="可留空" />
                </Field>
                <Select label="审核" value={form.moderation} onChange={(v) => setForm({ ...form, moderation: v })} options={["auto", "low"]} />
              </>
            )}
          </SettingsGroup>
        </aside>

        <section className="stage">
          <div className="stageHead">
            <div>
              <p><ModeIcon size={18} /> {modeMeta.title}</p>
              <h2>{form.mode === "chat" ? "像聊天一样连续生图" : "提交后生成图片到图库"}</h2>
            </div>
            {form.mode === "chat" && (
              <button className="ghostButton" onClick={newChat}><RefreshCw size={17} /> 新对话</button>
            )}
          </div>

          {error && <div className="errorBox">{error}</div>}

          {form.mode === "chat" ? (
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

          <form className="composer" onSubmit={handleSubmit}>
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
          </form>
        </section>

        {settingsOpen && (
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
        )}
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

function ImageCard({ image, onDownload }) {
  return (
    <div className="imageCard">
      <img src={image.url} alt="generated" />
      <div className="imageActions">
        <a href={image.url} target="_blank" rel="noreferrer">
          <ExternalLink size={14} />
          预览
        </a>
        <button type="button" onClick={() => onDownload(image)}>
          <Download size={14} />
          下载
        </button>
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
        {options.map((option) => <option key={option} value={option}>{option}</option>)}
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
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw data;
  return data;
}

function formatError(err) {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  if (err?.detail) return typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail, null, 2);
  return JSON.stringify(err, null, 2);
}

createRoot(document.getElementById("root")).render(<App />);
