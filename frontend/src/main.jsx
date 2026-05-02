import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  BookOpen,
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
  Plus,
  RefreshCw,
  Send,
  Sparkles,
  Trash2,
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

function modeLabel(mode) {
  return { chat: "对话", generate: "生图", edit: "编辑" }[mode] || mode;
}

function statusLabel(status) {
  return {
    queued: "排队中",
    running: "运行中",
    done: "已完成",
    failed: "失败",
    canceled: "已停止",
  }[status] || status;
}

function App() {
  const [config, setConfig] = useState(defaultConfig);
  const [providers, setProviders] = useState([]);
  const [providerDraft, setProviderDraft] = useState({ name: "", base_url: "", api_key: "" });
  const [editingProviderId, setEditingProviderId] = useState(null);
  const [modeProviders, setModeProviders] = useState({ chat: "", generate: "", edit: "" });
  const [form, setForm] = useState(() => ({
    ...defaults,
    prompt: "",
  }));
  const [controlsOpen, setControlsOpen] = useState(false);
  const [openGroups, setOpenGroups] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [galleryHistory, setGalleryHistory] = useState([]);
  const [prompts, setPrompts] = useState([]);
  const [promptDraft, setPromptDraft] = useState("");
  const [editingPromptId, setEditingPromptId] = useState(null);
  const [promptCopyId, setPromptCopyId] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [selectedHistory, setSelectedHistory] = useState(null);
  const [selectedTask, setSelectedTask] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [taskMeta, setTaskMeta] = useState({ active_count: 0, max_concurrent: 3 });
  const [activeView, setActiveView] = useState("studio");
  const [conversation, setConversation] = useState(null);
  const [messages, setMessages] = useState([]);
  const [studioSubmissions, setStudioSubmissions] = useState({ generate: [], edit: [] });
  const [editImages, setEditImages] = useState([]);
  const [editMask, setEditMask] = useState(null);
  const [chatImages, setChatImages] = useState([]);
  const [copied, setCopied] = useState("");
  const scrollRef = useRef(null);
  const taskStatusRef = useRef({});
  const dbSettingsReadyRef = useRef(false);
  const dbSettingsTimerRef = useRef(null);

  useEffect(() => {
    initializeSettings();
    refreshProviders();
  }, []);

  useEffect(() => {
    if (!dbSettingsReadyRef.current) return;
    if (dbSettingsTimerRef.current) clearTimeout(dbSettingsTimerRef.current);
    dbSettingsTimerRef.current = setTimeout(() => {
      saveAppSettings();
    }, 500);
    return () => {
      if (dbSettingsTimerRef.current) clearTimeout(dbSettingsTimerRef.current);
    };
  }, [config, form, modeProviders]);

  useEffect(() => {
    refreshHistory();
    refreshGallery();
    refreshPrompts();
    refreshTasks();
  }, []);

  useEffect(() => {
    const timer = setInterval(() => {
      refreshTasks();
      refreshHistory();
    }, 1800);
    return () => clearInterval(timer);
  }, [selectedTask?.id, conversation?.id, activeView]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading]);

  const runningTasks = tasks.filter((task) => ["queued", "running"].includes(task.status));
  const atTaskLimit = runningTasks.length >= (taskMeta.max_concurrent || 3);
  const submitDisabled = loading || !form.prompt.trim() || atTaskLimit;
  const activeProvider = providerForMode(form.mode);

  function switchView(view) {
    setActiveView(view);
    if (window.innerWidth <= 760) {
      setControlsOpen(false);
    }
  }

  function providerForMode(mode) {
    const providerId = modeProviders[mode];
    return providers.find((provider) => String(provider.id) === String(providerId)) || providers[0] || null;
  }

  function configForMode(mode) {
    const provider = providerForMode(mode);
    if (!provider) return config;
    return { base_url: provider.base_url, api_key: provider.api_key };
  }

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
    await startTask();
  }

  async function startTask() {
    if (atTaskLimit) {
      setError(describeError({ detail: { message: `已有 ${taskMeta.max_concurrent || 3} 个任务运行或排队，请等待完成后再创建新任务。` } }));
      return;
    }
    setLoading(true);
    try {
      if (form.mode === "generate") {
        rememberStudioSubmission("generate", form.prompt);
        await runGenerate();
      } else if (form.mode === "edit") {
        if (!editImages.length) throw new Error("编辑模式至少上传一张图片");
        rememberStudioSubmission("edit", form.prompt, [...editImages]);
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

  function rememberStudioSubmission(mode, prompt, files = []) {
    if (!["generate", "edit"].includes(mode)) return;
    const previews = files.map((file) => ({
      name: file.name,
      url: URL.createObjectURL(file),
    }));
    setStudioSubmissions((current) => ({
      ...current,
      [mode]: [
        ...current[mode],
        {
          id: `${mode}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
          prompt,
          previews,
          created_at: new Date().toISOString(),
        },
      ],
    }));
  }

  function newStudioTask() {
    if (form.mode === "chat") {
      newChat();
      return;
    }
    setStudioSubmissions((current) => ({ ...current, [form.mode]: [] }));
    setForm((value) => ({ ...value, prompt: "" }));
    if (form.mode === "edit") {
      setEditImages([]);
      setEditMask(null);
    }
    setError(null);
    setActiveView("studio");
  }

  async function runGenerate() {
    const runConfig = configForMode("generate");
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
      config: runConfig,
    };
    const res = await fetch(`${API}/api/images/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await parse(res);
    mergeTask(data.task);
    await refreshTasks();
    await refreshHistory();
    await refreshPrompts();
    return data.task;
  }

  async function runEdit() {
    if (!editImages.length) {
      throw new Error("编辑模式至少上传一张图片");
    }
    const data = new FormData();
    const runConfig = configForMode("edit");
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
      config: runConfig,
    };
    data.append("params_json", JSON.stringify(params));
    [...editImages].forEach((file) => data.append("images", file));
    if (editMask) data.append("mask", editMask);
    const res = await fetch(`${API}/api/images/edit`, { method: "POST", body: data });
    const result = await parse(res);
    mergeTask(result.task);
    await refreshTasks();
    await refreshHistory();
    await refreshPrompts();
    return result.task;
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
    const runConfig = configForMode("chat");
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
      config: runConfig,
    };
    data.append("params_json", JSON.stringify(params));
    [...chatImages].forEach((file) => data.append("images", file));
    const res = await fetch(`${API}/api/conversations/${active.id}/messages`, {
      method: "POST",
      body: data,
    });
    const result = await parse(res);
    setMessages((items) => items.map((item) => (item.id === localUser.id ? { ...item, id: result.user_message_id } : item)));
    mergeTask(result.task);
    setChatImages([]);
    await refreshHistory();
    await refreshTasks();
    await refreshPrompts();
    return result.task;
  }

  async function loadAppSettings() {
    try {
      const res = await fetch(`${API}/api/app-settings`);
      const data = await parse(res);
      const value = data.value || {};
      if (value.config) {
        setConfig({ ...defaultConfig, ...value.config });
      }
      if (value.form) {
        setForm({ ...normalizeFormSettings({ ...defaults, ...value.form }), prompt: "" });
      }
      if (value.modeProviders) {
        setModeProviders({ chat: "", generate: "", edit: "", ...value.modeProviders });
      }
    } catch (err) {
      setError(describeError(err));
    } finally {
      dbSettingsReadyRef.current = true;
    }
  }

  async function initializeSettings() {
    try {
      const res = await fetch(`${API}/api/settings`);
      const data = await parse(res);
      setConfig({
        base_url: data.base_url || defaultConfig.base_url,
        api_key: data.api_key || "",
      });
    } catch {
      setConfig(defaultConfig);
    }
    await loadAppSettings();
  }

  async function saveAppSettings() {
    const value = {
      config,
      form: persistableForm(form),
      modeProviders,
    };
    try {
      await fetch(`${API}/api/app-settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
    } catch (err) {
      setError(describeError(err));
    }
  }

  function mergeTask(task) {
    if (!task) return;
    setTasks((items) => [task, ...items.filter((item) => item.id !== task.id)]);
  }

  async function refreshTasks() {
    try {
      const res = await fetch(`${API}/api/tasks?limit=80`);
      const data = await parse(res);
      const items = data.items || [];
      const completedNow = items.some((task) => {
        const previous = taskStatusRef.current[task.id];
        return previous && previous !== task.status && ["done", "failed", "canceled"].includes(task.status);
      });
      taskStatusRef.current = Object.fromEntries(items.map((task) => [task.id, task.status]));
      setTasks(items);
      setTaskMeta({ active_count: data.active_count || 0, max_concurrent: data.max_concurrent || 3 });
      if (selectedTask) {
        const latestSelected = items.find((task) => task.id === selectedTask.id);
        if (latestSelected) setSelectedTask(latestSelected);
      }
      const activeConversationTask = activeView === "studio" && form.mode === "chat" && conversation && items.some(
        (task) => task.mode === "chat" && task.conversation_id === conversation.id && ["queued", "running", "done"].includes(task.status)
      );
      if (completedNow || activeConversationTask) {
        refreshGallery();
        if (activeConversationTask) loadConversation(conversation.id, { openStudio: true });
      }
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function cancelTask(taskId) {
    const res = await fetch(`${API}/api/tasks/${taskId}/cancel`, { method: "POST" });
    const data = await parse(res);
    mergeTask(data.task);
    await refreshTasks();
  }

  async function saveSettings() {
    const res = await fetch(`${API}/api/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    await parse(res);
    await saveAppSettings();
    setCopied("配置已保存");
    setTimeout(() => setCopied(""), 1400);
  }

  async function refreshProviders() {
    try {
      const res = await fetch(`${API}/api/providers`);
      const data = await parse(res);
      const items = data.items || [];
      setProviders(items);
      if (items.length) {
        const ids = new Set(items.map((provider) => String(provider.id)));
        setModeProviders((current) => ({
          chat: ids.has(String(current.chat)) ? current.chat : String(items[0].id),
          generate: ids.has(String(current.generate)) ? current.generate : String(items[0].id),
          edit: ids.has(String(current.edit)) ? current.edit : String(items[0].id),
        }));
      }
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function saveProvider() {
    const payload = {
      name: providerDraft.name.trim(),
      base_url: providerDraft.base_url.trim(),
      api_key: providerDraft.api_key.trim(),
    };
    if (!payload.name || !payload.base_url) {
      setError(describeError({ detail: { message: "提供商名称和接口地址不能为空" } }));
      return;
    }
    const url = editingProviderId ? `${API}/api/providers/${editingProviderId}` : `${API}/api/providers`;
    const res = await fetch(url, {
      method: editingProviderId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const saved = await parse(res);
    await refreshProviders();
    setProviderDraft({ name: "", base_url: "", api_key: "" });
    setEditingProviderId(null);
    setCopied(`已保存 ${saved.name}`);
    setTimeout(() => setCopied(""), 1400);
  }

  function editProvider(provider) {
    setProviderDraft({ name: provider.name, base_url: provider.base_url, api_key: provider.api_key });
    setEditingProviderId(provider.id);
  }

  async function deleteProvider(providerId) {
    const res = await fetch(`${API}/api/providers/${providerId}`, { method: "DELETE" });
    await parse(res);
    setModeProviders((current) => {
      const fallback = providers.find((provider) => provider.id !== providerId);
      const fallbackId = fallback ? String(fallback.id) : "";
      return {
        chat: String(current.chat) === String(providerId) ? fallbackId : current.chat,
        generate: String(current.generate) === String(providerId) ? fallbackId : current.generate,
        edit: String(current.edit) === String(providerId) ? fallbackId : current.edit,
      };
    });
    await refreshProviders();
  }

  function syncProviderToAll(providerId) {
    setModeProviders({ chat: providerId, generate: providerId, edit: providerId });
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

  async function refreshPrompts() {
    try {
      const res = await fetch(`${API}/api/prompts?limit=300`);
      const data = await parse(res);
      setPrompts(data.items || []);
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function savePromptEntry() {
    try {
      const content = promptDraft.trim();
      if (!content) {
        setError(describeError({ detail: { message: "提示词内容不能为空" } }));
        return;
      }
      const url = editingPromptId ? `${API}/api/prompts/${editingPromptId}` : `${API}/api/prompts`;
      const res = await fetch(url, {
        method: editingPromptId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, source: "manual", mode: null }),
      });
      await parse(res);
      setPromptDraft("");
      setEditingPromptId(null);
      await refreshPrompts();
    } catch (err) {
      setError(describeError(err));
    }
  }

  function editPromptEntry(item) {
    setPromptDraft(item.content || "");
    setEditingPromptId(item.id);
  }

  async function deletePromptEntry(id) {
    try {
      const res = await fetch(`${API}/api/prompts/${id}`, { method: "DELETE" });
      await parse(res);
      if (editingPromptId === id) {
        setPromptDraft("");
        setEditingPromptId(null);
      }
      await refreshPrompts();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function copyPromptEntry(item) {
    try {
      await copyTextToClipboard(item.content || "");
      setPromptCopyId(item.id);
      setTimeout(() => setPromptCopyId(null), 1400);
    } catch (err) {
      setError(describeError(err));
    }
  }

  function usePromptEntry(item) {
    setForm((value) => ({ ...value, prompt: item.content || "" }));
    switchView("studio");
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
    const hydrated = (data.messages || []).map((msg) => ({
      ...msg,
      images: imagesByMessage.get(msg.id) || [],
      uploaded_images: (msg.uploaded_images || []).map((image) => ({
        ...image,
        url: image.public_url || image.url,
        filename: image.file_path?.split(/[\\/]/).pop() || image.filename || "uploaded-image.png",
      })),
    }));
    setConversation(data.conversation);
    setMessages(hydrated);
    setForm((value) => ({ ...value, mode: "chat", context_limit: data.conversation.context_limit ?? value.context_limit }));
    setSelectedHistory({ ...data, messages: hydrated });
    setSelectedTask(null);
    if (openStudio) setActiveView("studio");
  }

  async function loadTask(taskId) {
    const res = await fetch(`${API}/api/tasks/${taskId}`);
    const data = await parse(res);
    setSelectedTask(data.task);
    setSelectedHistory(null);
    setActiveView("history");
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
            summary={activeProvider ? `当前：${activeProvider.name}` : (config.base_url || "未配置")}
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
            title="提供商管理"
            summary={activeProvider ? `${modeLabel(form.mode)}：${activeProvider.name}` : "未配置提供商"}
            open={!!openGroups.providers}
            onToggle={() => toggleGroup("providers")}
          >
            <Select
              label="对话模式"
              value={modeProviders.chat}
              onChange={(v) => setModeProviders({ ...modeProviders, chat: v })}
              options={providers.map((provider) => ({ value: String(provider.id), label: provider.name }))}
            />
            <Select
              label="生成模式"
              value={modeProviders.generate}
              onChange={(v) => setModeProviders({ ...modeProviders, generate: v })}
              options={providers.map((provider) => ({ value: String(provider.id), label: provider.name }))}
            />
            <Select
              label="编辑模式"
              value={modeProviders.edit}
              onChange={(v) => setModeProviders({ ...modeProviders, edit: v })}
              options={providers.map((provider) => ({ value: String(provider.id), label: provider.name }))}
            />
            <button className="secondaryButton" type="button" onClick={() => syncProviderToAll(modeProviders[form.mode] || String(providers[0]?.id || ""))}>
              <Check size={17} />
              当前提供商同步到全部模式
            </button>

            <div className="providerEditor">
              <Field label="提供商名称">
                <input value={providerDraft.name} onChange={(e) => setProviderDraft({ ...providerDraft, name: e.target.value })} placeholder="例如 asxs / OpenAI / 备用线路" />
              </Field>
              <Field label="接口地址">
                <input value={providerDraft.base_url} onChange={(e) => setProviderDraft({ ...providerDraft, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
              </Field>
              <Field label="密钥">
                <input type="password" value={providerDraft.api_key} onChange={(e) => setProviderDraft({ ...providerDraft, api_key: e.target.value })} placeholder="sk-..." />
              </Field>
              <div className="providerActions">
                <button className="secondaryButton" type="button" onClick={saveProvider}>
                  <KeyRound size={16} />
                  {editingProviderId ? "保存修改" : "新建提供商"}
                </button>
                {editingProviderId && (
                  <button className="ghostButton" type="button" onClick={() => { setEditingProviderId(null); setProviderDraft({ name: "", base_url: "", api_key: "" }); }}>
                    <X size={16} />
                    取消
                  </button>
                )}
              </div>
            </div>

            <div className="providerList">
              {providers.map((provider) => (
                <article className="providerItem" key={provider.id}>
                  <div>
                    <strong>{provider.name}</strong>
                    <small>{provider.base_url}</small>
                  </div>
                  <div>
                    <button type="button" onClick={() => editProvider(provider)} title="编辑"><Edit3 size={15} /></button>
                    <button type="button" onClick={() => deleteProvider(provider.id)} title="删除"><Trash2 size={15} /></button>
                  </div>
                </article>
              ))}
            </div>
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
              ["prompts", BookOpen, "提示词"],
            ].map(([value, Icon, label]) => (
              <button key={value} className={activeView === value ? "active" : ""} onClick={() => switchView(value)}>
                <Icon size={16} />
                {label}
              </button>
            ))}
          </nav>
          <div className="stageHead">
            <div>
              <p><ModeIcon size={18} /> {modeMeta.title}</p>
              <h2>{activeView === "history" ? "对话历史可查看和修改" : activeView === "gallery" ? "历史图片按对话和时间保存" : activeView === "prompts" ? "维护可复制的提示词库" : form.mode === "chat" ? "像聊天一样连续生图" : "提交后生成图片到图库"}</h2>
            </div>
            {activeView === "studio" && (
              <div className="headActions">
                {form.mode === "chat" && <button className="ghostButton" onClick={newChat}><RefreshCw size={17} /> 新对话</button>}
                {form.mode !== "chat" && <button className="ghostButton" onClick={newStudioTask}>
                  <Plus size={17} /> 新任务
                </button>}
              </div>
            )}
          </div>

          {error && <ErrorPanel error={error} onClose={() => setError(null)} />}

          {activeView === "history" ? (
            <HistoryPane
              conversations={conversations}
              tasks={tasks}
              selected={selectedHistory}
              selectedTask={selectedTask}
              onRefresh={async () => {
                await refreshHistory();
                await refreshTasks();
              }}
              onOpen={(id) => loadConversation(id, { openStudio: false })}
              onOpenTask={loadTask}
              onContinue={(id) => loadConversation(id, { openStudio: true })}
              onSaveMeta={saveConversationMeta}
              onSaveMessage={saveMessage}
              onDownload={downloadImage}
              onUseImage={useImageAsReference}
              onCancelTask={cancelTask}
            />
          ) : activeView === "gallery" ? (
            <GalleryHistory items={galleryHistory} onRefresh={refreshGallery} onDownload={downloadImage} onUseImage={useImageAsReference} />
          ) : activeView === "prompts" ? (
            <PromptLibrary
              items={prompts}
              draft={promptDraft}
              editingId={editingPromptId}
              copiedId={promptCopyId}
              onDraft={setPromptDraft}
              onSave={savePromptEntry}
              onCancel={() => { setPromptDraft(""); setEditingPromptId(null); }}
              onEdit={editPromptEntry}
              onDelete={deletePromptEntry}
              onCopy={copyPromptEntry}
              onUse={usePromptEntry}
              onRefresh={refreshPrompts}
            />
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
                  <div className="bubble">任务已提交到后台，可以切换页面或开启其它任务。</div>
                </div>
              )}
            </div>
          ) : (
            <Gallery items={studioSubmissions[form.mode] || []} loading={loading} onDownload={downloadImage} />
          )}

          {activeView === "studio" && <form className="composer" onSubmit={handleSubmit}>
            {form.mode === "edit" && (
              <UploadRow
                label="编辑图片"
                files={editImages}
                onChange={setEditImages}
                onRemove={(index) => setEditImages((items) => items.filter((_, i) => i !== index))}
                multiple
              />
            )}
            {form.mode === "edit" && (
              <UploadRow
                label="Mask"
                files={editMask ? [editMask] : []}
                onChange={(files) => setEditMask(files[0] || null)}
                onRemove={() => setEditMask(null)}
              />
            )}
            {form.mode === "chat" && (
              <UploadRow
                label="参考图片"
                files={chatImages}
                onChange={setChatImages}
                onRemove={(index) => setChatImages((items) => items.filter((_, i) => i !== index))}
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
  const [copyState, setCopyState] = useState("idle");
  const detailText = error.displayDetail || error.detail || error.raw || error.summary;
  const copyText = error.copyDetail || error.detail || error.raw || error.summary;

  async function copyError() {
    setCopyState("copying");
    try {
      await copyTextToClipboard(copyText);
      setCopyState("success");
    } catch {
      setCopyState("failed");
    }
    setTimeout(() => setCopyState("idle"), 1400);
  }

  return (
    <section className="errorPanel">
      <div className="errorPanelHead">
        <div>
          <strong>生图失败</strong>
          <span>{error.summary}</span>
        </div>
        <div className="errorActions">
          <button className={`copyFeedbackButton ${copyState}`} type="button" onClick={copyError} disabled={copyState === "copying"}>
            <Copy size={15} /> {copyState === "copying" ? "复制中" : copyState === "success" ? "复制成功" : copyState === "failed" ? "复制失败" : "复制原因"}
          </button>
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

function HistoryPane({
  conversations,
  tasks,
  selected,
  selectedTask,
  onRefresh,
  onOpen,
  onOpenTask,
  onContinue,
  onSaveMeta,
  onSaveMessage,
  onDownload,
  onUseImage,
  onCancelTask,
}) {
  const [draftTitle, setDraftTitle] = useState("");
  const [draftLimit, setDraftLimit] = useState(10);
  const [messageDrafts, setMessageDrafts] = useState({});
  const records = useMemo(() => buildHistoryRecords(conversations, tasks), [conversations, tasks]);
  const conversationTasks = selected?.conversation
    ? (tasks || []).filter((task) => Number(task.conversation_id) === Number(selected.conversation.id))
    : [];

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
          <strong>历史记录</strong>
          <button type="button" onClick={onRefresh}><RefreshCw size={15} />刷新</button>
        </div>
        {records.length === 0 ? (
          <div className="emptyMini">暂无历史记录</div>
        ) : records.map((item) => (
          <button
            key={item.key}
            className={`historyItem ${historyRecordActive(item, selected, selectedTask) ? "active" : ""} ${item.status || "idle"}`}
            onClick={() => item.kind === "conversation" ? onOpen(item.id) : onOpenTask(item.id)}
          >
            <div className="historyItemTop">
              <span title={item.title}>{item.title}</span>
              <StatusPill mode={item.mode} status={item.status} />
            </div>
            <small>{item.summary}</small>
            {item.status && (
              <div className="historyProgress">
                <div style={{ width: `${Math.max(4, Math.min(Number(item.progress || 0), 100))}%` }} />
              </div>
            )}
            <small>{item.stage || item.timeLabel}</small>
          </button>
        ))}
      </div>
      <div className="historyDetail">
        {selectedTask ? (
          <TaskDetail
            task={selectedTask}
            onCancel={onCancelTask}
            onDownload={onDownload}
            onUseImage={onUseImage}
            onContinue={onContinue}
          />
        ) : !selected ? (
          <div className="emptyState">
            <FolderOpen size={34} />
            <h3>选择一段历史</h3>
            <p>打开后可以查看任务状态、失败原因、历史图片，也可以继续对话或继续改图。</p>
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
            {conversationTasks.length > 0 && (
              <section className="conversationTasks">
                <div className="sectionTitle">
                  <strong>本对话任务</strong>
                  <small>{conversationTasks.length} 条</small>
                </div>
                <div className="taskMiniList">
                  {conversationTasks.map((task) => (
                    <TaskMiniRow key={task.id} task={task} onOpenTask={onOpenTask} onCancelTask={onCancelTask} />
                  ))}
                </div>
              </section>
            )}
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
                  {msg.uploaded_images?.length > 0 && (
                    <div className="imageGrid uploadedImageGrid">
                      {msg.uploaded_images.map((image) => (
                        <ImageCard key={image.url} image={image} onDownload={onDownload} />
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

function buildHistoryRecords(conversations, tasks) {
  const conversationRecords = (conversations || []).map((item) => ({
    key: `conversation-${item.id}`,
    kind: "conversation",
    id: item.id,
    mode: "chat",
    title: item.title || "未命名对话",
    status: item.latest_task_status,
    progress: item.latest_task_progress,
    stage: item.latest_task_stage,
    time: item.updated_at || item.created_at,
    timeLabel: formatTime(item.updated_at || item.created_at),
    summary: `${item.message_count || 0} 条消息 / ${item.image_count || 0} 张图`,
  }));
  const taskRecords = (tasks || [])
    .filter((task) => !task.conversation_id)
    .map((task) => ({
      key: `task-${task.id}`,
      kind: "task",
      id: task.id,
      mode: task.mode,
      title: task.prompt || `${modeLabel(task.mode)}任务 #${task.id}`,
      status: task.status,
      progress: task.progress,
      stage: task.stage,
      time: task.updated_at || task.created_at,
      timeLabel: formatTime(task.updated_at || task.created_at),
      summary: `${modeLabel(task.mode)}任务 #${task.id}`,
    }));
  return [...conversationRecords, ...taskRecords].sort((a, b) => new Date(b.time || 0) - new Date(a.time || 0));
}

function historyRecordActive(item, selected, selectedTask) {
  if (item.kind === "conversation") {
    return selected?.conversation?.id === item.id && !selectedTask;
  }
  return selectedTask?.id === item.id;
}

function StatusPill({ mode, status }) {
  return (
    <span className={`statusPill ${status || "idle"}`}>
      <b>{modeLabel(mode)}</b>
      <em>{status ? statusLabel(status) : "记录"}</em>
    </span>
  );
}

function TaskMiniRow({ task, onOpenTask, onCancelTask }) {
  const isLive = ["queued", "running"].includes(task.status);
  return (
    <article className={`taskMiniRow ${task.status}`}>
      <button type="button" onClick={() => onOpenTask(task.id)}>
        <span>{modeLabel(task.mode)}任务 #{task.id}</span>
        <small>{task.stage || statusLabel(task.status)} · {Number(task.progress || 0)}%</small>
      </button>
      <div className="progressTrack">
        <div style={{ width: `${Math.max(4, Math.min(Number(task.progress || 0), 100))}%` }} />
      </div>
      {isLive && <button type="button" onClick={() => onCancelTask(task.id)} title="停止任务"><X size={14} /></button>}
    </article>
  );
}

function TaskDetail({ task, onCancel, onDownload, onUseImage, onContinue }) {
  const [copyState, setCopyState] = useState("idle");
  const isLive = ["queued", "running"].includes(task.status);
  const images = normalizeTaskImages(task);
  const errorText = task.error_detail ? formatTaskError(task.error_detail) : "";

  async function copyError() {
    setCopyState("copying");
    try {
      await copyTextToClipboard(errorText || "暂无失败原因");
      setCopyState("success");
    } catch {
      setCopyState("failed");
    }
    setTimeout(() => setCopyState("idle"), 1400);
  }

  return (
    <div className="taskDetail">
      <div className="taskDetailHead">
        <div>
          <StatusPill mode={task.mode} status={task.status} />
          <h3>{task.prompt || `${modeLabel(task.mode)}任务 #${task.id}`}</h3>
          <p>#{task.id} · {task.stage || statusLabel(task.status)} · {Number(task.progress || 0)}%</p>
        </div>
        <div className="taskDetailActions">
          {isLive && <button className="ghostButton danger" type="button" onClick={() => onCancel(task.id)}><X size={16} /> 停止</button>}
          {task.conversation_id && (
            <button className="secondaryButton compact" type="button" onClick={() => onContinue(task.conversation_id)}>
              <MessageCircle size={16} /> 继续对话
            </button>
          )}
        </div>
      </div>
      <div className="progressTrack large">
        <div style={{ width: `${Math.max(4, Math.min(Number(task.progress || 0), 100))}%` }} />
      </div>
      <div className="taskFacts">
        <span>创建：{formatTime(task.created_at)}</span>
        <span>更新：{formatTime(task.updated_at)}</span>
        <span>模式：{modeLabel(task.mode)}</span>
      </div>
      {task.error_detail && (
        <section className="taskErrorBox">
          <div className="sectionTitle">
            <strong>失败原因</strong>
            <button className={`copyFeedbackButton ${copyState}`} type="button" onClick={copyError} disabled={copyState === "copying"}>
              <Copy size={15} /> {copyState === "copying" ? "复制中" : copyState === "success" ? "复制成功" : copyState === "failed" ? "复制失败" : "复制原因"}
            </button>
          </div>
          <pre>{errorText}</pre>
        </section>
      )}
      {images.length > 0 ? (
        <section>
          <div className="sectionTitle">
            <strong>生成图片</strong>
            <small>{images.length} 张</small>
          </div>
          <div className="imageGrid">
            {images.map((image) => <ImageCard key={image.id || image.url} image={image} onDownload={onDownload} onUseImage={onUseImage} />)}
          </div>
        </section>
      ) : (
        <div className="emptyMini detailEmpty">这个任务还没有可查看的图片。</div>
      )}
    </div>
  );
}

function PromptLibrary({
  items,
  draft,
  editingId,
  copiedId,
  onDraft,
  onSave,
  onCancel,
  onEdit,
  onDelete,
  onCopy,
  onUse,
  onRefresh,
}) {
  return (
    <div className="promptLibrary">
      <section className="promptEditor">
        <div className="paneToolbar">
          <strong>{editingId ? "修改提示词" : "新增提示词"}</strong>
          <button type="button" onClick={onRefresh}><RefreshCw size={15} />刷新</button>
        </div>
        <textarea
          value={draft}
          onChange={(event) => onDraft(event.target.value)}
          placeholder="写入一条常用提示词，只保存文字，不保存图片。"
        />
        <div className="promptEditorActions">
          <button className="secondaryButton compact" type="button" onClick={onSave}>
            <Check size={16} /> {editingId ? "保存修改" : "保存到库"}
          </button>
          {editingId && (
            <button className="ghostButton" type="button" onClick={onCancel}>
              <X size={16} /> 取消
            </button>
          )}
        </div>
      </section>

      {items.length === 0 ? (
        <div className="emptyState">
          <BookOpen size={34} />
          <h3>提示词库还是空的</h3>
          <p>之后每次对话、生图、编辑都会自动保存文字提示词，也可以手动新增。</p>
        </div>
      ) : (
        <div className="promptGrid">
          {items.map((item) => (
            <article className="promptCard" key={item.id}>
              <div className="promptCardMeta">
                <span>{item.source === "auto" ? "自动保存" : "手动保存"}</span>
                <small>{item.mode ? modeLabel(item.mode) : "通用"} · {formatTime(item.created_at)}</small>
              </div>
              <p>{item.content}</p>
              <div className="promptCardActions">
                <button className={`copyFeedbackButton ${copiedId === item.id ? "success" : ""}`} type="button" onClick={() => onCopy(item)}>
                  <Copy size={15} /> {copiedId === item.id ? "复制成功" : "复制"}
                </button>
                <button type="button" onClick={() => onUse(item)}><Send size={15} /> 使用</button>
                <button type="button" onClick={() => onEdit(item)}><Edit3 size={15} /> 修改</button>
                <button type="button" onClick={() => onDelete(item.id)}><Trash2 size={15} /> 删除</button>
              </div>
            </article>
          ))}
        </div>
      )}
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
        {msg.uploaded_images?.length > 0 && (
          <div className="imageGrid uploadedImageGrid">
            {msg.uploaded_images.map((image) => <ImageCard key={image.url} image={image} onDownload={onDownload} />)}
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
          <h3>当前工作台是空的</h3>
          <p>历史图片不再显示在这里。新任务会进入历史记录，生成完成后也可以在图库查看。</p>
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
          <span>已提交到后台</span>
          <h3>{item.prompt}</h3>
          <small>{formatTime(item.created_at)}，可切换页面或继续提交其它任务。</small>
          {item.previews?.length > 0 && (
            <div className="imageGrid uploadedImageGrid">
              {item.previews.map((image) => <img key={image.url} src={image.url} alt={image.name || "uploaded"} />)}
            </div>
          )}
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

function UploadRow({ label, files, onChange, onRemove, multiple = false }) {
  const [previews, setPreviews] = useState([]);

  useEffect(() => {
    const next = files.map((file) => ({ file, url: URL.createObjectURL(file) }));
    setPreviews(next);
    return () => next.forEach((item) => URL.revokeObjectURL(item.url));
  }, [files]);

  return (
    <div className="uploadRow">
      <label className="uploadPicker">
        <span><ImagePlus size={16} /> {label}</span>
        <input
          type="file"
          accept="image/*"
          multiple={multiple}
          onChange={(event) => onChange([...event.target.files])}
        />
      </label>
      <div className="uploadPreviewList">
        {previews.length ? previews.map((item, index) => (
          <div className="uploadPreview" key={`${item.file.name}-${index}`}>
            <img src={item.url} alt={item.file.name} />
            <small>{item.file.name}</small>
            <button type="button" onClick={() => onRemove?.(index)} title="删除图片"><X size={14} /></button>
          </div>
        )) : <small className="uploadEmpty">未选择</small>}
      </div>
    </div>
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
  const fullDetail = JSON.stringify(detail, null, 2);
  return {
    summary,
    meta,
    detail: fullDetail,
    displayDetail: JSON.stringify(compactErrorForDisplay(detail), null, 2),
    copyDetail: fullDetail,
    raw: JSON.stringify(err, null, 2),
  };
}

function compactErrorForDisplay(value) {
  if (typeof value === "string") {
    return compactString(value);
  }
  if (Array.isArray(value)) {
    return value.map(compactErrorForDisplay);
  }
  if (value && typeof value === "object") {
    const next = {};
    for (const [key, item] of Object.entries(value)) {
      if (key === "raw" && typeof item === "string" && looksLikeHtml(item)) {
        next[key] = "[上游返回 HTML 错误页，完整内容可点击“复制原因”获取]";
      } else {
        next[key] = compactErrorForDisplay(item);
      }
    }
    return next;
  }
  return value;
}

function compactString(value) {
  if (looksLikeHtml(value)) return "[上游返回 HTML 错误页，完整内容可点击“复制原因”获取]";
  return value.length > 1600 ? `${value.slice(0, 1600)}\n...[已截断，完整内容可复制]` : value;
}

function looksLikeHtml(value) {
  const text = value.slice(0, 400).toLowerCase();
  return text.includes("<!doctype html") || text.includes("<html") || text.includes("<head");
}

function formatTaskError(error) {
  return typeof error === "string"
    ? compactString(error)
    : JSON.stringify(compactErrorForDisplay(error), null, 2);
}

async function copyTextToClipboard(text) {
  const value = String(text ?? "");
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      // Fall through to the legacy path for HTTP deployments and strict mobile browsers.
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  const ok = document.execCommand("copy");
  textarea.remove();
  if (!ok) throw new Error("复制失败，请手动选择文本复制。");
  return true;
}

function normalizeTaskImages(task) {
  return (task?.images || []).map((image) => ({
    ...image,
    url: image.public_url || image.url,
    filename: image.file_path?.split(/[\\/]/).pop() || image.filename || "generated-image.png",
  }));
}

function formatTime(value) {
  if (!value) return "未知时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
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
