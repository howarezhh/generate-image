import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  BookOpen,
  Bot,
  Brush,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clapperboard,
  Clock3,
  Copy,
  Download,
  Edit3,
  Eraser,
  ExternalLink,
  FolderOpen,
  Heart,
  ImagePlus,
  Images,
  KeyRound,
  Loader2,
  MessageCircle,
  Plus,
  RefreshCw,
  Send,
  Search,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Wand2,
  X,
} from "lucide-react";
import "./styles.css";

const API = "";
const APP_SETTINGS_VERSION = 5;

const defaultConfig = {
  base_url: "https://api.asxs.top/v1",
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
  { value: "2560x1440", label: "电脑横屏 2K 高清" },
  { value: "1440x2560", label: "手机竖屏 2K 高清" },
  { value: "1536x1024", label: "横屏 3:2 稳定" },
  { value: "1024x1536", label: "竖屏 2:3 稳定" },
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

const plannerEndpointOptions = [
  { value: "responses", label: "Responses（默认）" },
  { value: "chat_completions", label: "Chat Completions" },
];

const referenceRoleOptions = [
  { value: "character", label: "角色" },
  { value: "scene", label: "场景" },
  { value: "wardrobe_prop", label: "服装道具" },
  { value: "style", label: "风格" },
];

const modeOptions = [
  { value: "chat", icon: MessageCircle, label: "对话", help: "对话生图：像聊天一样描述、追问和完善想法，AI 会在合适时机生成或编辑图片。" },
  { value: "storyboard", icon: Clapperboard, label: "分镜", help: "分镜连续生图：为视频镜头逐张生成首帧，并用上一镜头画面保持人物、场景和逻辑连续。" },
  { value: "generate", icon: Wand2, label: "生成", help: "普通生图：直接根据一份图片提示词生成独立图片，适合一次性创作。" },
  { value: "edit", icon: Brush, label: "编辑", help: "图片编辑：上传参考图后按你的描述改图、续画或调整画面。" },
];

const defaults = {
  mode: "chat",
  prompt: "",
  model: "gpt-5.4",
  chatModel: "gpt-5.4",
  plannerEndpoint: "responses",
  imageModel: "gpt-image-2",
  action: "auto",
  size: "2560x1440",
  quality: "high",
  n: 1,
  background: "auto",
  output_format: "png",
  output_compression: "",
  moderation: "auto",
  input_fidelity: "auto",
  partial_images: 0,
  context_limit: 10,
  shot_limit: 20,
};

function persistableForm(form) {
  const { prompt, ...settings } = form;
  return settings;
}

function normalizeFormSettings(settings) {
  const next = { ...settings };
  if (!String(next.chatModel || "").trim()) {
    next.chatModel = defaults.chatModel;
  }
  if (!plannerEndpointOptions.some((option) => option.value === next.plannerEndpoint)) {
    next.plannerEndpoint = defaults.plannerEndpoint;
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
  const shotLimit = Number(next.shot_limit);
  if (!Number.isFinite(shotLimit) || shotLimit < 1 || shotLimit > 100) {
    next.shot_limit = defaults.shot_limit;
  }
  return next;
}

function uniqueProviderIds(values) {
  const seen = new Set();
  const ids = [];
  for (const value of Array.isArray(values) ? values : []) {
    const id = String(value || "").trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    ids.push(id);
  }
  return ids;
}

function optionLabel(options, value) {
  return options.find((option) => option.value === value)?.label || value;
}

function modeLabel(mode) {
  return { chat: "对话", storyboard: "分镜", generate: "生图", edit: "编辑" }[mode] || mode;
}

function continueLabel(mode) {
  return { chat: "继续对话", storyboard: "继续分镜", generate: "继续生图", edit: "继续编辑" }[mode] || "继续";
}

function taskProviderName(task) {
  return (
    task?.image_provider_name ||
    task?.response?.raw?.image_provider?.name ||
    task?.response?.image_provider?.name ||
    ""
  );
}

function isConversationalMode(mode) {
  return ["chat", "storyboard"].includes(mode);
}

function isSessionMode(mode) {
  return ["chat", "storyboard", "generate", "edit"].includes(mode);
}

function resolveConversationMode(conversation) {
  if (!conversation) return "";
  const mode = String(conversation.mode || conversation.latest_task_mode || "").trim();
  return isSessionMode(mode) ? mode : "chat";
}

function defaultReferenceRole(index) {
  return ["character", "scene", "wardrobe_prop"][index] || "style";
}

function referenceRoleLabel(role) {
  return referenceRoleOptions.find((option) => option.value === role)?.label || "风格";
}

function uploadFileRoleKey(file) {
  return `${file.name}-${file.size}-${file.lastModified}`;
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
  const [modeProviders, setModeProviders] = useState({ chat: "", storyboard: "", generate: "", edit: "" });
  const [plannerProviders, setPlannerProviders] = useState({ chat: "", storyboard: "" });
  const [imageProviderPool, setImageProviderPool] = useState([]);
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
  const [promptDraftMode, setPromptDraftMode] = useState("");
  const [promptFilter, setPromptFilter] = useState({ q: "", mode: "", favorite: false });
  const [editingPromptId, setEditingPromptId] = useState(null);
  const [promptCopyId, setPromptCopyId] = useState(null);
  const [refreshFeedback, setRefreshFeedback] = useState({});
  const [sectionSaveFeedback, setSectionSaveFeedback] = useState({});
  const [conversations, setConversations] = useState([]);
  const [selectedHistory, setSelectedHistory] = useState(null);
  const [selectedTask, setSelectedTask] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [taskMeta, setTaskMeta] = useState({
    active_count: 0,
    max_concurrent: 3,
    image_provider_pool: {
      total_providers: 0,
      used_providers: 0,
      idle_providers: 0,
      limit_per_provider: 3,
      total_capacity: 3,
      providers: [],
    },
  });
  const [activeView, setActiveView] = useState("studio");
  const [conversation, setConversation] = useState(null);
  const [messages, setMessages] = useState([]);
  const [studioSubmissions, setStudioSubmissions] = useState({ generate: [], edit: [] });
  const [editImages, setEditImages] = useState([]);
  const [editMask, setEditMask] = useState(null);
  const [chatImages, setChatImages] = useState([]);
  const [chatReferenceImages, setChatReferenceImages] = useState([]);
  const [chatUploadRoles, setChatUploadRoles] = useState({});
  const [chatReferenceRoles, setChatReferenceRoles] = useState({});
  const [previewState, setPreviewState] = useState(null);
  const [runningPanelOpen, setRunningPanelOpen] = useState(false);
  const scrollRef = useRef(null);
  const taskStatusRef = useRef({});
  const dbSettingsReadyRef = useRef(false);
  const dbSettingsTimerRef = useRef(null);
  const activeViewRef = useRef(activeView);
  const formModeRef = useRef(form.mode);
  const conversationRef = useRef(conversation);
  const selectedTaskRef = useRef(selectedTask);
  const conversationLoadSeqRef = useRef(0);
  const taskEventSourcesRef = useRef(new Map());
  const taskEventRetryTimersRef = useRef(new Map());
  const taskEventAttemptsRef = useRef(new Map());
  const taskEventConversationIdsRef = useRef(new Map());
  const tasksRef = useRef(tasks);

  useEffect(() => {
    activeViewRef.current = activeView;
    formModeRef.current = form.mode;
    conversationRef.current = conversation;
    selectedTaskRef.current = selectedTask;
    tasksRef.current = tasks;
  }, [activeView, form.mode, conversation, selectedTask, tasks]);

  useEffect(() => {
    initializeSettings();
    refreshProviders();
    return () => {
      taskEventSourcesRef.current.forEach((source) => source.close());
      taskEventSourcesRef.current.clear();
      taskEventRetryTimersRef.current.forEach((timer) => clearTimeout(timer));
      taskEventRetryTimersRef.current.clear();
      taskEventAttemptsRef.current.clear();
      taskEventConversationIdsRef.current.clear();
    };
  }, []);

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
    const liveTaskIds = new Set(tasks.filter((task) => ["queued", "running"].includes(task.status)).map((task) => Number(task.id)));
    tasks
      .filter((task) => ["queued", "running"].includes(task.status))
      .forEach((task) => {
        const taskId = Number(task.id);
        if (!taskEventSourcesRef.current.has(taskId) && !taskEventRetryTimersRef.current.has(taskId)) {
          startTaskEventStream(taskId, task.conversation_id || null);
        }
      });
    taskEventSourcesRef.current.forEach((_source, taskId) => {
      if (!liveTaskIds.has(Number(taskId))) {
        closeTaskEventStream(taskId);
      }
    });
    taskEventRetryTimersRef.current.forEach((_timer, taskId) => {
      if (!liveTaskIds.has(Number(taskId))) {
        closeTaskEventStream(taskId);
      }
    });
  }, [tasks]);

  useEffect(() => {
    if (activeView === "prompts") {
      refreshPrompts();
    }
  }, [promptFilter.q, promptFilter.mode, promptFilter.favorite, activeView]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading]);

  const runningTasks = tasks.filter((task) => ["queued", "running"].includes(task.status));
  const atTaskLimit = runningTasks.length >= (taskMeta.max_concurrent || 3);
  const activeConversationMode = resolveConversationMode(conversation);
  const activePlannerProvider = ["chat", "storyboard"].includes(form.mode) ? providerForPlannerMode(form.mode) : null;
  const imageProviderPoolMeta = taskMeta.image_provider_pool || { total_providers: 0, used_providers: 0, idle_providers: 0, limit_per_provider: 3, total_capacity: 3, providers: [] };
  const chatGeneratedImages = useMemo(() => uniqueImages(
    messages.flatMap((msg) => msg.images || [])
  ), [messages]);
  const liveConversationTasks = useMemo(() => {
    if (!conversation) return [];
    return tasks.filter((task) => Number(task.conversation_id) === Number(conversation.id) && ["queued", "running"].includes(task.status));
  }, [tasks, conversation]);
  const activeConversationLocked = isSessionMode(form.mode) && !!conversation && activeConversationMode === form.mode && liveConversationTasks.length > 0;
  const submitDisabled = loading || !form.prompt.trim() || atTaskLimit || activeConversationLocked;

  function switchView(view) {
    if (view !== "studio") {
      conversationLoadSeqRef.current += 1;
    }
    setActiveView(view);
    if (window.innerWidth <= 760) {
      setControlsOpen(false);
    }
  }

  function providerForPlannerMode(mode) {
    const providerId = plannerProviders[mode] || modeProviders[mode];
    return providers.find((provider) => String(provider.id) === String(providerId)) || null;
  }

  function configForMode(mode) {
    return { ...config };
  }

  function openImagePreview(images, index = 0) {
    const items = uniqueImages(images).filter((image) => image.url || image.public_url);
    if (!items.length) return;
    const safeIndex = Math.max(0, Math.min(index, items.length - 1));
    setPreviewState({ items, index: safeIndex });
  }

  function closeImagePreview() {
    setPreviewState(null);
  }

  function moveImagePreview(offset) {
    setPreviewState((current) => {
      if (!current?.items?.length) return current;
      const nextIndex = (current.index + offset + current.items.length) % current.items.length;
      return { ...current, index: nextIndex };
    });
  }

  function plannerConfigForMode(mode) {
    const provider = providerForPlannerMode(mode);
    if (!provider) return configForMode(mode);
    return { base_url: provider.base_url, api_key: provider.api_key };
  }

  async function ensureConversation(targetMode = formModeRef.current) {
    const currentConversation = conversationRef.current;
    if (currentConversation && resolveConversationMode(currentConversation) === targetMode) return currentConversation;
    const res = await fetch(`${API}/api/conversations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: form.prompt.slice(0, 24) || "新的生图对话",
        context_limit: Number(form.context_limit),
        mode: isSessionMode(targetMode) ? targetMode : null,
      }),
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

  function handlePromptKeyDown(event) {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.nativeEvent?.isComposing) return;
    event.preventDefault();
    if (submitDisabled) return;
    event.currentTarget.form?.requestSubmit();
  }

  async function runRefresh(key, action) {
    setRefreshFeedback((current) => ({ ...current, [key]: "loading" }));
    try {
      await action();
      setRefreshFeedback((current) => ({ ...current, [key]: "success" }));
    } catch (err) {
      setError(describeError(err));
      setRefreshFeedback((current) => ({ ...current, [key]: "failed" }));
    } finally {
      setTimeout(() => {
        setRefreshFeedback((current) => {
          if (current[key] === "loading") return current;
          const next = { ...current };
          delete next[key];
          return next;
        });
      }, 1300);
    }
  }

  function selectedReferenceCount(nextUploads = chatImages, nextReferences = chatReferenceImages) {
    return (nextUploads?.length || 0) + (nextReferences?.length || 0);
  }

  function uploadRoleFor(file, index, roles = chatUploadRoles) {
    const key = uploadFileRoleKey(file, index);
    return roles[key] || defaultReferenceRole(index);
  }

  function selectedReferenceRoleFor(image, index, roles = chatReferenceRoles, uploadCount = chatImages.length) {
    return roles[String(image.id)] || defaultReferenceRole(uploadCount + index);
  }

  function normalizeUploadRoles(files, currentRoles = chatUploadRoles) {
    const next = {};
    files.forEach((file, index) => {
      const key = uploadFileRoleKey(file, index);
      next[key] = uploadRoleFor(file, index, currentRoles);
    });
    return next;
  }

  function updateChatUploads(files) {
    const room = Math.max(0, 3 - chatReferenceImages.length);
    const trimmed = files.slice(0, room);
    setChatImages(trimmed);
    setChatUploadRoles((current) => normalizeUploadRoles(trimmed, current));
    if (files.length > room) {
      setError(describeError({ detail: { message: "对话模式最多同时指定 3 张参考图。" } }));
    }
  }

  function updateChatUploadRole(file, index, role) {
    const key = uploadFileRoleKey(file, index);
    setChatUploadRoles((current) => ({ ...current, [key]: role }));
  }

  function updateSelectedReferenceRole(imageId, role) {
    setChatReferenceRoles((current) => ({ ...current, [String(imageId)]: role }));
  }

  function toggleChatReferenceImage(image) {
    setChatReferenceImages((items) => {
      const exists = items.some((item) => Number(item.id) === Number(image.id));
      if (exists) {
        setChatReferenceRoles((current) => {
          const next = { ...current };
          delete next[String(image.id)];
          return next;
        });
        return items.filter((item) => Number(item.id) !== Number(image.id));
      }
      if (selectedReferenceCount(chatImages, items) >= 3) {
        setError(describeError({ detail: { message: "最多选择 3 张参考图，请先移除一张。" } }));
        return items;
      }
      setChatReferenceRoles((current) => ({
        ...current,
        [String(image.id)]: current[String(image.id)] || defaultReferenceRole(chatImages.length + items.length),
      }));
      return [...items, image];
    });
  }

  function removeChatReferenceImage(imageId) {
    setChatReferenceImages((items) => items.filter((item) => Number(item.id) !== Number(imageId)));
    setChatReferenceRoles((current) => {
      const next = { ...current };
      delete next[String(imageId)];
      return next;
    });
  }

  async function startTask() {
    if (atTaskLimit) {
      setError(describeError({ detail: { message: `已有 ${taskMeta.max_concurrent || 3} 个任务运行或排队，请等待完成后再创建新任务。` } }));
      return;
    }
    if (activeConversationLocked) {
      setError(describeError({ detail: { message: `当前${modeLabel(form.mode)}会话仍有任务运行中，请先停止该会话任务，或点击“新任务”新开对话后再继续发送。` } }));
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
      } else if (form.mode === "storyboard") {
        await runStoryboard();
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
    if (isSessionMode(form.mode)) {
      newChat(form.mode);
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
    await saveAppSettings({ throwError: true });
    const active = await ensureConversation("generate");
    const localUser = {
      id: `u-${Date.now()}`,
      role: "user",
      content: form.prompt,
      images: [],
      uploaded_images: [],
    };
    setMessages((items) => [...items, localUser]);
    const runConfig = configForMode("generate");
    const body = {
      prompt: form.prompt,
      conversation_id: active.id,
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
    if (data.user_message_id) {
      setMessages((items) => items.map((item) => (item.id === localUser.id ? { ...item, id: data.user_message_id } : item)));
    }
    mergeTask(data.task);
    await refreshTasks();
    await refreshHistory();
    await refreshPrompts();
    await loadConversation(active.id, { openStudio: true, autoRefresh: true });
    return data.task;
  }

  async function runEdit() {
    if (!editImages.length) {
      throw new Error("编辑模式至少上传一张图片");
    }
    await saveAppSettings({ throwError: true });
    const active = await ensureConversation("edit");
    const localUploads = [...editImages].map((file) => {
      const previewUrl = URL.createObjectURL(file);
      return {
        id: `upload-${file.name}-${Date.now()}`,
        url: previewUrl,
        public_url: previewUrl,
        filename: file.name,
      };
    });
    const localUser = {
      id: `u-${Date.now()}`,
      role: "user",
      content: form.prompt,
      images: [],
      uploaded_images: localUploads,
    };
    setMessages((items) => [...items, localUser]);
    const data = new FormData();
    const runConfig = configForMode("edit");
    const params = {
      prompt: form.prompt,
      conversation_id: active.id,
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
    if (result.user_message_id) {
      setMessages((items) => items.map((item) => (item.id === localUser.id ? { ...item, id: result.user_message_id } : item)));
    }
    mergeTask(result.task);
    await refreshTasks();
    await refreshHistory();
    await refreshPrompts();
    await loadConversation(active.id, { openStudio: true, autoRefresh: true });
    return result.task;
  }

  async function runChat() {
    await saveAppSettings({ throwError: true });
    const active = await ensureConversation("chat");
    const localUser = {
      id: `u-${Date.now()}`,
      role: "user",
      content: form.prompt,
      previews: [...chatImages].map((file) => URL.createObjectURL(file)),
      uploaded_images: chatReferenceImages,
    };
    setMessages((items) => [...items, localUser]);

    const data = new FormData();
    const runConfig = configForMode("chat");
    const params = {
      prompt: form.prompt,
      model: form.model,
      planner_model: form.chatModel.trim() || null,
      planner_endpoint: form.plannerEndpoint,
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
      reference_image_ids: chatReferenceImages.map((image) => image.id),
      reference_image_roles: Object.fromEntries(chatReferenceImages.map((image, index) => [String(image.id), selectedReferenceRoleFor(image, index)])),
      upload_reference_roles: chatImages.map((file, index) => uploadRoleFor(file, index)),
      config: runConfig,
      planner_config: plannerConfigForMode("chat"),
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
    startTaskEventStream(result.task?.id, active.id);
    setChatImages([]);
    setChatUploadRoles({});
    await refreshHistory();
    await refreshTasks();
    await refreshPrompts();
    return result.task;
  }

  async function runStoryboard() {
    await saveAppSettings({ throwError: true });
    const active = await ensureConversation("storyboard");
    const localUser = {
      id: `u-${Date.now()}`,
      role: "user",
      content: form.prompt,
      previews: [...chatImages].map((file) => URL.createObjectURL(file)),
      uploaded_images: chatReferenceImages,
    };
    setMessages((items) => [...items, localUser]);

    const data = new FormData();
    const runConfig = configForMode("storyboard");
    const params = {
      prompt: form.prompt,
      model: form.model,
      planner_model: form.chatModel.trim() || null,
      planner_endpoint: form.plannerEndpoint,
      image_model: form.imageModel,
      size: form.size,
      quality: form.quality,
      background: form.background,
      output_format: form.output_format,
      output_compression: form.output_compression === "" ? null : Number(form.output_compression),
      moderation: form.moderation,
      input_fidelity: form.input_fidelity === "auto" ? "high" : form.input_fidelity,
      partial_images: Number(form.partial_images),
      context_limit: Number(form.context_limit),
      shot_limit: Number(form.shot_limit),
      reference_image_ids: chatReferenceImages.map((image) => image.id),
      reference_image_roles: Object.fromEntries(chatReferenceImages.map((image, index) => [String(image.id), selectedReferenceRoleFor(image, index)])),
      upload_reference_roles: chatImages.map((file, index) => uploadRoleFor(file, index)),
      config: runConfig,
      planner_config: plannerConfigForMode("storyboard"),
    };
    data.append("params_json", JSON.stringify(params));
    [...chatImages].forEach((file) => data.append("images", file));
    const res = await fetch(`${API}/api/storyboards/${active.id}/messages`, {
      method: "POST",
      body: data,
    });
    const result = await parse(res);
    setMessages((items) => items.map((item) => (item.id === localUser.id ? { ...item, id: result.user_message_id } : item)));
    mergeTask(result.task);
    startTaskEventStream(result.task?.id, active.id);
    setChatImages([]);
    setChatUploadRoles({});
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
        const savedForm = { ...value.form };
        if (!value.settings_version && savedForm.size === "1536x1024") {
          savedForm.size = defaults.size;
        }
        setForm({ ...normalizeFormSettings({ ...defaults, ...savedForm }), prompt: "" });
      }
      if (value.modeProviders) {
        setModeProviders({ chat: "", storyboard: "", generate: "", edit: "", ...value.modeProviders });
      }
      if (value.imageProviderPool) {
        setImageProviderPool(uniqueProviderIds(value.imageProviderPool));
      } else if (value.modeProviders) {
        setImageProviderPool(uniqueProviderIds(Object.values(value.modeProviders)));
      }
      if (value.plannerProviders) {
        setPlannerProviders({ chat: "", storyboard: "", ...value.plannerProviders });
      } else if (value.modeProviders) {
        setPlannerProviders({ chat: value.modeProviders.chat || "", storyboard: value.modeProviders.storyboard || "" });
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

  async function saveAppSettings(options = {}) {
    if (dbSettingsTimerRef.current) {
      clearTimeout(dbSettingsTimerRef.current);
      dbSettingsTimerRef.current = null;
    }
    const value = {
      settings_version: APP_SETTINGS_VERSION,
      config,
      form: persistableForm(form),
      modeProviders,
      plannerProviders,
      imageProviderPool,
    };
    try {
      await fetch(`${API}/api/app-settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
    } catch (err) {
      setError(describeError(err));
      if (options.throwError) throw err;
    }
  }

  function mergeTask(task) {
    if (!task) return;
    setTasks((items) => [task, ...items.filter((item) => item.id !== task.id)]);
    setSelectedTask((current) => (current && Number(current.id) === Number(task.id) ? { ...current, ...task } : current));
  }

  function closeTaskEventSource(taskId) {
    const source = taskEventSourcesRef.current.get(Number(taskId));
    if (source) {
      source.close();
      taskEventSourcesRef.current.delete(Number(taskId));
    }
  }

  function clearTaskEventRetry(taskId) {
    const timer = taskEventRetryTimersRef.current.get(Number(taskId));
    if (timer) {
      clearTimeout(timer);
      taskEventRetryTimersRef.current.delete(Number(taskId));
    }
  }

  function closeTaskEventStream(taskId) {
    closeTaskEventSource(taskId);
    clearTaskEventRetry(taskId);
    taskEventAttemptsRef.current.delete(Number(taskId));
    taskEventConversationIdsRef.current.delete(Number(taskId));
  }

  function parseEventData(event) {
    try {
      return JSON.parse(event.data || "{}");
    } catch {
      return {};
    }
  }

  function upsertStreamingMessage(message, conversationId) {
    if (!message || Number(conversationRef.current?.id) !== Number(conversationId || message.conversation_id)) return;
    setMessages((items) => {
      const normalized = {
        ...message,
        meta: message.meta || {},
        images: message.images || [],
        uploaded_images: message.uploaded_images || [],
      };
      const exists = items.some((item) => Number(item.id) === Number(normalized.id));
      if (exists) {
        return items.map((item) => (Number(item.id) === Number(normalized.id) ? { ...item, ...normalized } : item));
      }
      return [...items, normalized];
    });
  }

  function updateStreamingMessageContent(messageId, content, conversationId) {
    if (Number(conversationRef.current?.id) !== Number(conversationId)) return;
    setMessages((items) => items.map((item) => (
      Number(item.id) === Number(messageId) ? { ...item, content } : item
    )));
  }

  function updateStreamingMessageMeta(messageId, meta, conversationId) {
    if (Number(conversationRef.current?.id) !== Number(conversationId) || !meta || typeof meta !== "object") return;
    setMessages((items) => items.map((item) => {
      if (Number(item.id) !== Number(messageId)) return item;
      const nextMeta = { ...(item.meta || {}), ...meta };
      return {
        ...item,
        meta: nextMeta,
        image_error_detail: nextMeta.image_error || null,
        image_status: nextMeta.image_status || "",
        image_prompt: nextMeta.image_prompt || "",
      };
    }));
  }

  function appendStreamingMessageImage(messageId, image, conversationId) {
    if (!image || Number(conversationRef.current?.id) !== Number(conversationId)) return;
    const normalizedImage = normalizeImageForClient(image);
    setMessages((items) => items.map((item) => {
      if (Number(item.id) !== Number(messageId)) return item;
      return { ...item, images: uniqueImages([...(item.images || []), normalizedImage]) };
    }));
  }

  function taskIsLive(taskId) {
    return tasksRef.current.some((task) => Number(task.id) === Number(taskId) && ["queued", "running"].includes(task.status));
  }

  function scheduleTaskEventReconnect(taskId, conversationId) {
    if (!taskId || !taskIsLive(taskId) || taskEventRetryTimersRef.current.has(Number(taskId))) return;
    const attempt = (taskEventAttemptsRef.current.get(Number(taskId)) || 0) + 1;
    taskEventAttemptsRef.current.set(Number(taskId), attempt);
    const delay = Math.min(1000 * (2 ** (attempt - 1)), 10000);
    const timer = setTimeout(() => {
      taskEventRetryTimersRef.current.delete(Number(taskId));
      if (!taskIsLive(taskId)) {
        closeTaskEventStream(taskId);
        return;
      }
      startTaskEventStream(taskId, conversationId);
    }, delay);
    taskEventRetryTimersRef.current.set(Number(taskId), timer);
  }

  function startTaskEventStream(taskId, conversationId) {
    if (!taskId || typeof EventSource === "undefined") return;
    const normalizedTaskId = Number(taskId);
    const normalizedConversationId = conversationId ?? taskEventConversationIdsRef.current.get(normalizedTaskId) ?? null;
    taskEventConversationIdsRef.current.set(normalizedTaskId, normalizedConversationId);
    clearTaskEventRetry(normalizedTaskId);
    closeTaskEventSource(normalizedTaskId);
    const source = new EventSource(`${API}/api/tasks/${taskId}/events`);
    taskEventSourcesRef.current.set(normalizedTaskId, source);

    source.onopen = () => {
      clearTaskEventRetry(normalizedTaskId);
      taskEventAttemptsRef.current.set(normalizedTaskId, 0);
    };

    source.addEventListener("assistant_start", (event) => {
      const data = parseEventData(event);
      upsertStreamingMessage(data.message, normalizedConversationId);
    });
    source.addEventListener("assistant_reply", (event) => {
      const data = parseEventData(event);
      updateStreamingMessageContent(data.message_id, data.content || "", normalizedConversationId);
    });
    source.addEventListener("assistant_plan", (event) => {
      const data = parseEventData(event);
      updateStreamingMessageMeta(data.message_id, data.meta || {}, data.conversation_id || normalizedConversationId);
    });
    source.addEventListener("task_update", (event) => {
      const data = parseEventData(event);
      if (data.task) mergeTask(data.task);
    });
    source.addEventListener("storyboard_image", (event) => {
      const data = parseEventData(event);
      appendStreamingMessageImage(data.message_id, data.image, data.conversation_id);
    });
    for (const eventName of ["done", "failed", "canceled"]) {
      source.addEventListener(eventName, async () => {
        closeTaskEventStream(normalizedTaskId);
        await refreshTasks();
        await refreshHistory();
        await refreshPrompts();
      });
    }
    source.onerror = () => {
      closeTaskEventSource(normalizedTaskId);
      scheduleTaskEventReconnect(normalizedTaskId, normalizedConversationId);
    };
  }

  async function refreshTasks(options = {}) {
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
      setTaskMeta((current) => ({
        ...current,
        active_count: data.active_count || 0,
        max_concurrent: data.max_concurrent || 3,
        image_provider_pool: data.image_provider_pool || current.image_provider_pool,
      }));
      const currentSelectedTask = selectedTaskRef.current;
      if (currentSelectedTask) {
        const latestSelected = items.find((task) => task.id === currentSelectedTask.id);
        if (latestSelected) setSelectedTask(latestSelected);
      }
      const currentConversation = conversationRef.current;
      const canRefreshCurrentConversation = activeViewRef.current === "studio" && isSessionMode(formModeRef.current) && currentConversation;
      const activeConversationTask = canRefreshCurrentConversation && items.some(
        (task) => isSessionMode(task.mode) && task.conversation_id === currentConversation.id && ["queued", "running", "done"].includes(task.status)
      );
      if (completedNow || activeConversationTask) {
        refreshGallery();
        if (activeConversationTask) loadConversation(currentConversation.id, { openStudio: true, autoRefresh: true });
      }
    } catch (err) {
      setError(describeError(err));
      if (options.throwError) throw err;
    }
  }

  async function cancelTask(taskId) {
    const res = await fetch(`${API}/api/tasks/${taskId}/cancel`, { method: "POST" });
    const data = await parse(res);
    closeTaskEventStream(taskId);
    mergeTask(data.task);
    await refreshTasks();
  }

  async function retryTask(taskId) {
    await saveAppSettings({ throwError: true });
    const res = await fetch(`${API}/api/tasks/${taskId}/retry`, { method: "POST" });
    const data = await parse(res);
    mergeTask(data.task);
    await refreshTasks();
    await refreshHistory();
    await loadTask(data.task.id);
  }

  async function deleteTask(taskId) {
    if (!window.confirm("确认删除这条任务历史吗？这会移除本地数据库记录和该任务关联的图片文件。")) return;
    try {
      const res = await fetch(`${API}/api/tasks/${taskId}`, { method: "DELETE" });
      await parse(res);
      closeTaskEventStream(taskId);
      setSelectedTask(null);
      setTasks((items) => items.filter((task) => Number(task.id) !== Number(taskId)));
      await refreshTasks();
      await refreshHistory();
      await refreshGallery();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function deleteConversation(conversationId) {
    if (!window.confirm("确认删除这段对话历史吗？这会移除本地数据库记录、消息、任务和关联图片文件。")) return;
    try {
      const res = await fetch(`${API}/api/conversations/${conversationId}`, { method: "DELETE" });
      await parse(res);
      setSelectedHistory(null);
      if (conversation?.id === conversationId) {
        setConversation(null);
        setMessages([]);
        setChatImages([]);
        setChatReferenceImages([]);
        setChatUploadRoles({});
        setChatReferenceRoles({});
      }
      await refreshHistory();
      await refreshTasks();
      await refreshGallery();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function saveSettings() {
    const res = await fetch(`${API}/api/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    await parse(res);
    await saveAppSettings();
  }

  function setSettingsFeedback(section, state, message) {
    setSectionSaveFeedback((current) => ({ ...current, [section]: { state, message } }));
    window.setTimeout(() => {
      setSectionSaveFeedback((current) => {
        if (!current[section] || current[section].message !== message) return current;
        const next = { ...current };
        delete next[section];
        return next;
      });
    }, 1800);
  }

  async function saveSettingsSection(section, successMessage) {
    setSettingsFeedback(section, "loading", "正在保存...");
    try {
      await saveAppSettings({ throwError: true });
      setSettingsFeedback(section, "success", successMessage);
    } catch {
      setSettingsFeedback(section, "failed", "保存失败");
    }
  }

  async function refreshProviders() {
    try {
      const res = await fetch(`${API}/api/providers`);
      const data = await parse(res);
      const items = data.items || [];
      setProviders(items);
      const apiPoolIds = uniqueProviderIds((data.image_provider_pool?.providers || []).map((provider) => provider.id));
      setTaskMeta((current) => ({
        ...current,
        image_provider_pool: data.image_provider_pool || current.image_provider_pool,
        max_concurrent: data.image_provider_pool?.total_capacity || current.max_concurrent || 3,
      }));
      if (items.length) {
        const ids = new Set(items.map((provider) => String(provider.id)));
        setModeProviders((current) => ({
          chat: ids.has(String(current.chat)) ? current.chat : String(items[0].id),
          storyboard: ids.has(String(current.storyboard)) ? current.storyboard : String(items[0].id),
          generate: ids.has(String(current.generate)) ? current.generate : String(items[0].id),
          edit: ids.has(String(current.edit)) ? current.edit : String(items[0].id),
        }));
        setPlannerProviders((current) => ({
          chat: ids.has(String(current.chat)) ? current.chat : String(items[0].id),
          storyboard: ids.has(String(current.storyboard)) ? current.storyboard : String(items[0].id),
        }));
        setImageProviderPool((current) => {
          const next = uniqueProviderIds(current).filter((id) => ids.has(String(id)));
          if (next.length) return next;
          if (apiPoolIds.length) return apiPoolIds;
          return items.map((provider) => String(provider.id));
        });
      } else {
        setImageProviderPool([]);
      }
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function saveProvider() {
    try {
      const payload = {
        name: providerDraft.name.trim(),
        base_url: providerDraft.base_url.trim(),
        api_key: providerDraft.api_key.trim(),
      };
      if (!payload.name || !payload.base_url) {
        setError(describeError({ detail: { message: "提供商名称和接口地址不能为空" } }));
        setSettingsFeedback("providers", "failed", "保存失败");
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
      setSettingsFeedback("providers", "success", `已保存 ${saved.name}`);
    } catch (err) {
      setError(describeError(err));
      setSettingsFeedback("providers", "failed", "保存失败");
    }
  }

  function editProvider(provider) {
    setProviderDraft({ name: provider.name, base_url: provider.base_url, api_key: provider.api_key });
    setEditingProviderId(provider.id);
  }

  async function deleteProvider(providerId) {
    try {
      const res = await fetch(`${API}/api/providers/${providerId}`, { method: "DELETE" });
      await parse(res);
      setModeProviders((current) => {
        const fallback = providers.find((provider) => provider.id !== providerId);
        const fallbackId = fallback ? String(fallback.id) : "";
        return {
          chat: String(current.chat) === String(providerId) ? fallbackId : current.chat,
          storyboard: String(current.storyboard) === String(providerId) ? fallbackId : current.storyboard,
          generate: String(current.generate) === String(providerId) ? fallbackId : current.generate,
          edit: String(current.edit) === String(providerId) ? fallbackId : current.edit,
        };
      });
      setPlannerProviders((current) => {
        const fallback = providers.find((provider) => provider.id !== providerId);
        const fallbackId = fallback ? String(fallback.id) : "";
        return {
          chat: String(current.chat) === String(providerId) ? fallbackId : current.chat,
          storyboard: String(current.storyboard) === String(providerId) ? fallbackId : current.storyboard,
        };
      });
      setImageProviderPool((current) => {
        const next = current.filter((id) => String(id) !== String(providerId));
        if (next.length) return next;
        const fallback = providers.find((provider) => provider.id !== providerId);
        return fallback ? [String(fallback.id)] : [];
      });
      await refreshProviders();
      setSettingsFeedback("providers", "success", "提供商已删除");
    } catch (err) {
      setError(describeError(err));
      setSettingsFeedback("providers", "failed", "删除失败");
    }
  }

  function toggleImageProvider(providerId) {
    const normalizedId = String(providerId);
    setImageProviderPool((current) => {
      const exists = current.includes(normalizedId);
      if (exists && current.length <= 1) {
        setError(describeError({ detail: { message: "生图提供商池至少保留一个提供商。" } }));
        return current;
      }
      if (exists) return current.filter((id) => id !== normalizedId);
      return [...current, normalizedId];
    });
  }

  async function newChat(targetMode = formModeRef.current) {
    setConversation(null);
    setMessages([]);
    setChatImages([]);
    setChatReferenceImages([]);
    setChatUploadRoles({});
    setChatReferenceRoles({});
    setSelectedTask(null);
    setForm((value) => ({ ...value, mode: targetMode }));
    setActiveView("studio");
  }

  async function refreshHistory(options = {}) {
    try {
      const res = await fetch(`${API}/api/conversations`);
      const data = await parse(res);
      setConversations(data.items || []);
    } catch (err) {
      setError(describeError(err));
      if (options.throwError) throw err;
    }
  }

  async function refreshGallery(options = {}) {
    try {
      const res = await fetch(`${API}/api/gallery`);
      const data = await parse(res);
      setGalleryHistory(data.items || []);
    } catch (err) {
      setError(describeError(err));
      if (options.throwError) throw err;
    }
  }

  async function refreshPrompts(options = {}) {
    try {
      const params = new URLSearchParams({ limit: "300" });
      if (promptFilter.q.trim()) params.set("q", promptFilter.q.trim());
      if (promptFilter.mode) params.set("mode", promptFilter.mode);
      if (promptFilter.favorite) params.set("favorite", "1");
      const res = await fetch(`${API}/api/prompts?${params.toString()}`);
      const data = await parse(res);
      setPrompts(data.items || []);
    } catch (err) {
      setError(describeError(err));
      if (options.throwError) throw err;
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
        body: JSON.stringify({ content, source: "manual", mode: promptDraftMode || null }),
      });
      await parse(res);
      setPromptDraft("");
      setPromptDraftMode("");
      setEditingPromptId(null);
      await refreshPrompts();
    } catch (err) {
      setError(describeError(err));
    }
  }

  function editPromptEntry(item) {
    setPromptDraft(item.content || "");
    setPromptDraftMode(item.mode || "");
    setEditingPromptId(item.id);
  }

  async function togglePromptFavorite(item) {
    try {
      const res = await fetch(`${API}/api/prompts/${item.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: item.content || "",
          source: item.source || "manual",
          mode: item.mode || null,
          favorite: item.favorite ? 0 : 1,
        }),
      });
      await parse(res);
      await refreshPrompts();
    } catch (err) {
      setError(describeError(err));
    }
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

  async function loadConversation(id, { openStudio = true, autoRefresh = false } = {}) {
    const previousConversationId = conversationRef.current?.id;
    if (autoRefresh) {
      const currentConversation = conversationRef.current;
      if (activeViewRef.current !== "studio" || !isSessionMode(formModeRef.current) || currentConversation?.id !== id) {
        return;
      }
    }
    const loadSeq = ++conversationLoadSeqRef.current;
    const res = await fetch(`${API}/api/conversations/${id}`);
    const data = await parse(res);
    if (loadSeq !== conversationLoadSeqRef.current) return;
    if (autoRefresh) {
      const currentConversation = conversationRef.current;
      if (activeViewRef.current !== "studio" || !isSessionMode(formModeRef.current) || currentConversation?.id !== id) {
        return;
      }
    }
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
        source: image.source,
        prompt_text: image.prompt_text,
      });
      imagesByMessage.set(image.message_id, list);
    }
    const hydrated = (data.messages || []).map((msg) => {
      const meta = parseJsonObject(msg.meta_json);
      return {
        ...msg,
        meta,
        image_error_detail: meta.image_error || null,
        image_status: meta.image_status || "",
        image_prompt: meta.image_prompt || "",
        images: imagesByMessage.get(msg.id) || [],
        uploaded_images: (msg.uploaded_images || []).map((image) => ({
          ...image,
          url: image.public_url || image.url,
          filename: image.file_path?.split(/[\\/]/).pop() || image.filename || "uploaded-image.png",
        })),
      };
    });
    setSelectedHistory({ ...data, messages: hydrated });
    setSelectedTask(null);
    if (openStudio) {
      const switchingConversation = Number(previousConversationId) !== Number(data.conversation.id);
      const conversationMode = resolveConversationMode(data.conversation);
      setConversation({ ...data.conversation, mode: conversationMode });
      setMessages(hydrated);
      setForm((value) => ({ ...value, mode: conversationMode, context_limit: data.conversation.context_limit ?? value.context_limit }));
      if (switchingConversation) {
        setChatImages([]);
        setChatReferenceImages([]);
        setChatUploadRoles({});
        setChatReferenceRoles({});
      } else {
        setChatReferenceImages((items) => items.filter((image) => Number(image.conversation_id) === Number(data.conversation.id)));
        setChatReferenceRoles((current) => Object.fromEntries(
          Object.entries(current).filter(([imageId]) => (data.images || []).some((image) => String(image.id) === String(imageId)))
        ));
      }
      setActiveView("studio");
    }
  }

  async function loadTask(taskId) {
    conversationLoadSeqRef.current += 1;
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

  async function historyImageToFile(image) {
    const response = await fetch(image.public_url || image.url);
    const blob = await response.blob();
    const filename = image.file_path?.split(/[\\/]/).pop() || image.filename || "history-image.png";
    return new File([blob], filename, { type: image.mime_type || blob.type || "image/png" });
  }

  async function useImageAsReference(image) {
    const targetMode = image.task_mode === "storyboard" ? "storyboard" : "chat";
    let useConversationReference = false;
    if (image.conversation_id) {
      await loadConversation(image.conversation_id, { openStudio: true });
      if (resolveConversationMode(conversationRef.current) !== targetMode) {
        await newChat(targetMode);
      } else if (image.id && image.source === "api") {
        useConversationReference = true;
      }
    } else {
      setActiveView("studio");
      setForm((value) => ({ ...value, mode: targetMode }));
    }
    if (useConversationReference) {
      const normalized = normalizeImageForClient(image);
      setChatReferenceImages([normalized]);
      setChatReferenceRoles({ [String(normalized.id)]: "character" });
      setChatImages([]);
      setChatUploadRoles({});
    } else {
      const file = await historyImageToFile(image);
      setChatImages([file]);
      setChatUploadRoles({ [uploadFileRoleKey(file, 0)]: "character" });
      setChatReferenceImages([]);
      setChatReferenceRoles({});
    }
    setActiveView("studio");
    setForm((value) => ({ ...value, prompt: "", mode: targetMode, action: targetMode === "chat" ? "edit" : value.action }));
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
    if (form.mode === "storyboard") return { icon: Clapperboard, title: "分镜连续生图" };
    return { icon: MessageCircle, title: "对话生图" };
  }, [form.mode]);
  const ModeIcon = modeMeta.icon;

  return (
    <>
    <main className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brandMark"><Sparkles size={22} /></div>
          <div>
            <h1>GPT Image Studio</h1>
            <span>个人生图工作台</span>
          </div>
        </div>
      </header>

      <section className={`workspace ${controlsOpen ? "withControls" : "withoutControls"}`}>
        {controlsOpen && (
        <aside className="controls" id="studio-controls">
          <div className="modeSwitch">
            {modeOptions.map(({ value, icon: Icon, label, help }) => (
              <button
                key={value}
                className={form.mode === value ? "active" : ""}
                onClick={() => {
                  if (isSessionMode(value) && conversationRef.current && resolveConversationMode(conversationRef.current) !== value) {
                    newChat(value);
                    return;
                  }
                  setForm((f) => ({ ...f, mode: value }));
                }}
                title={help}
                aria-label={`${label}：${help}`}
              >
                <Icon size={17} />
                {label}
              </button>
            ))}
          </div>

          <SettingsGroup
            title="提供商管理"
            summary={`生图池 ${imageProviderPoolMeta.total_providers || imageProviderPool.length || providers.length} 个 / 已用 ${imageProviderPoolMeta.used_providers || 0} / 空闲 ${imageProviderPoolMeta.idle_providers || 0}${activePlannerProvider ? ` / 当前规划：${activePlannerProvider.name}` : ""}`}
            open={!!openGroups.providers}
            onToggle={() => toggleGroup("providers")}
          >
            <div className="providerModeGrid">
              <Select
                className="fieldCompact"
                label="对话规划"
                help="负责理解对话、判断是否生图、撰写单张图片提示词的模型线路。"
                value={plannerProviders.chat}
                onChange={(v) => setPlannerProviders({ ...plannerProviders, chat: v })}
                options={providers.map((provider) => ({ value: String(provider.id), label: provider.name }))}
              />
              <Select
                className="fieldCompact"
                label="分镜规划"
                help="负责规划人物场景概述、镜头列表和每张首帧提示词的模型线路。"
                value={plannerProviders.storyboard}
                onChange={(v) => setPlannerProviders({ ...plannerProviders, storyboard: v })}
                options={providers.map((provider) => ({ value: String(provider.id), label: provider.name }))}
              />
            </div>
            <div className="providerPoolStats">
              <span>池中总数 {imageProviderPoolMeta.total_providers || imageProviderPool.length || providers.length}</span>
              <span>已使用 {imageProviderPoolMeta.used_providers || 0}</span>
              <span>空闲 {imageProviderPoolMeta.idle_providers || 0}</span>
              <span>单提供商上限 {imageProviderPoolMeta.limit_per_provider || 3}</span>
            </div>

            <div className="providerEditor">
              <div className="providerEditorGrid">
              <Field className="fieldCompact" label="提供商名称">
                <input value={providerDraft.name} onChange={(e) => setProviderDraft({ ...providerDraft, name: e.target.value })} placeholder="例如 asxs / OpenAI / 备用线路" />
              </Field>
              <Field className="fieldCompact" label="接口地址">
                <input value={providerDraft.base_url} onChange={(e) => setProviderDraft({ ...providerDraft, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
              </Field>
              <Field className="fieldFull" label="密钥">
                <input type="password" value={providerDraft.api_key} onChange={(e) => setProviderDraft({ ...providerDraft, api_key: e.target.value })} placeholder="sk-..." />
              </Field>
              </div>
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
                    <label className="providerPoolToggle">
                      <input
                        type="checkbox"
                        checked={imageProviderPool.includes(String(provider.id))}
                        onChange={() => toggleImageProvider(provider.id)}
                      />
                      <span>加入生图池</span>
                    </label>
                    <strong>{provider.name}</strong>
                    <small>{provider.base_url}</small>
                    <small>当前任务 {provider.pool_assigned_tasks || 0} / 运行中 {provider.pool_running_tasks || 0} / 空闲槽位 {provider.pool_idle_slots ?? (imageProviderPoolMeta.limit_per_provider || 3)}</small>
                  </div>
                  <div>
                    <button type="button" onClick={() => editProvider(provider)} title="编辑"><Edit3 size={15} /></button>
                    <button type="button" onClick={() => deleteProvider(provider.id)} title="删除"><Trash2 size={15} /></button>
                  </div>
                </article>
              ))}
            </div>
            <SettingsSaveAction
              feedback={sectionSaveFeedback.providers}
              label="保存提供商选择"
              onClick={() => saveSettingsSection("providers", "提供商选择已保存")}
            />
          </SettingsGroup>

          <SettingsGroup
            title="模型设置"
            summary={["chat", "storyboard"].includes(form.mode) ? `规划 ${form.chatModel} · ${optionLabel(plannerEndpointOptions, form.plannerEndpoint)} / 生图 ${form.model} + ${form.imageModel}` : `${form.model} / ${form.imageModel}`}
            open={!!openGroups.models}
            onToggle={() => toggleGroup("models")}
          >
            {["chat", "storyboard"].includes(form.mode) ? (
              <>
                <Field label="规划模型">
                  <input value={form.chatModel} onChange={(e) => setForm({ ...form, chatModel: e.target.value })} placeholder="例如 qwen-plus / deepseek-chat / gpt-5.4" />
                </Field>
                <Select
                  label="规划接口格式"
                  help="默认用 Responses；只有规划模型不支持 Responses 时，才显式切到 Chat Completions。生图执行始终走 Responses。"
                  value={form.plannerEndpoint}
                  onChange={(v) => setForm({ ...form, plannerEndpoint: v })}
                  options={plannerEndpointOptions}
                />
                <Select label="生图 Responses 模型" value={form.model} onChange={(v) => setForm({ ...form, model: v })} options={chatModelOptions} />
                <Select label="图片工具模型" value={form.imageModel} onChange={(v) => setForm({ ...form, imageModel: v })} options={imageModelOptions} />
              </>
            ) : (
              <>
                <Select label="Responses 模型" value={form.model} onChange={(v) => setForm({ ...form, model: v })} options={chatModelOptions} />
                <Select label="图片工具模型" value={form.imageModel} onChange={(v) => setForm({ ...form, imageModel: v })} options={imageModelOptions} />
              </>
            )}
            <SettingsSaveAction
              feedback={sectionSaveFeedback.models}
              label="保存模型设置"
              onClick={() => saveSettingsSection("models", "模型设置已保存")}
            />
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
            {!["chat", "storyboard"].includes(form.mode) && (
              <Field label="数量">
                <input type="number" min="1" max="10" value={form.n} onChange={(e) => setForm({ ...form, n: e.target.value })} />
              </Field>
            )}
            <SettingsSaveAction
              feedback={sectionSaveFeedback.image}
              label="保存图片参数"
              onClick={() => saveSettingsSection("image", "图片参数已保存")}
            />
          </SettingsGroup>

          <SettingsGroup
            title="高级选项"
            summary={["chat", "storyboard"].includes(form.mode) ? `${form.mode === "storyboard" ? `${form.shot_limit} 镜头 / ` : `${optionLabel(actionOptions, form.action)} / `}${optionLabel(fidelityOptions, form.input_fidelity)}` : optionLabel(moderationOptions, form.moderation)}
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
            ) : form.mode === "storyboard" ? (
              <>
                <Select label="输入保真" value={form.input_fidelity} onChange={(v) => setForm({ ...form, input_fidelity: v })} options={fidelityOptions} />
                <Select label="局部图" value={String(form.partial_images)} onChange={(v) => setForm({ ...form, partial_images: Number(v) })} options={["0", "1", "2", "3"]} />
                <Field label="上下文条数">
                  <input type="number" min="0" max="50" value={form.context_limit} onChange={(e) => setForm({ ...form, context_limit: e.target.value })} />
                </Field>
                <Field label="最多镜头">
                  <input type="number" min="1" max="100" value={form.shot_limit} onChange={(e) => setForm({ ...form, shot_limit: e.target.value })} />
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
            <SettingsSaveAction
              feedback={sectionSaveFeedback.advanced}
              label="保存高级选项"
              onClick={() => saveSettingsSection("advanced", "高级选项已保存")}
            />
          </SettingsGroup>
        </aside>
        )}

        <section className="stage">
          <div className="viewTabsBar">
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
              <button
                type="button"
                className={`settingsToggle ${controlsOpen ? "active" : ""}`}
                onClick={() => setControlsOpen((value) => !value)}
                aria-expanded={controlsOpen}
                aria-controls="studio-controls"
              >
                <SlidersHorizontal size={16} />
                设置
              </button>
            </nav>
          </div>
          <div className="stageHead">
            <div>
              <p><ModeIcon size={18} /> {modeMeta.title}</p>
              <h2>{activeView === "history" ? "对话历史可查看和修改" : activeView === "gallery" ? "历史图片按对话和时间保存" : activeView === "prompts" ? "维护可复制的提示词库" : form.mode === "storyboard" ? "按镜头顺序生成连续首帧" : form.mode === "chat" ? "像聊天一样连续生图" : form.mode === "edit" ? "同一会话内直接按完整提示词连续编辑" : "同一会话内直接按完整提示词连续生图"}</h2>
            </div>
            {(activeView === "studio" || runningTasks.length > 0) && (
              <div className="headActions">
                {runningTasks.length > 0 && (
                  <RunningTasksPanel
                    tasks={runningTasks}
                    open={runningPanelOpen}
                    onToggle={() => setRunningPanelOpen((value) => !value)}
                    onOpenTask={loadTask}
                    onCancelTask={cancelTask}
                    poolMeta={imageProviderPoolMeta}
                  />
                )}
                {activeView === "studio" && isSessionMode(form.mode) && <button className="ghostButton" onClick={newStudioTask}><Plus size={17} /> {form.mode === "storyboard" ? "新分镜会话" : form.mode === "chat" ? "新对话" : form.mode === "generate" ? "新生图会话" : "新编辑会话"}</button>}
                {activeView === "studio" && !isSessionMode(form.mode) && <button className="ghostButton" onClick={newStudioTask}>
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
              refreshState={refreshFeedback.history}
              onRefresh={() => runRefresh("history", async () => {
                await refreshHistory({ throwError: true });
                await refreshTasks({ throwError: true });
              })}
              onOpen={(id) => loadConversation(id, { openStudio: false })}
              onOpenTask={loadTask}
              onContinue={(id) => loadConversation(id, { openStudio: true })}
              onSaveMeta={saveConversationMeta}
              onSaveMessage={saveMessage}
              onDownload={downloadImage}
              onUseImage={useImageAsReference}
              onPreview={openImagePreview}
              onCancelTask={cancelTask}
              onRetryTask={retryTask}
              onDeleteTask={deleteTask}
              onDeleteConversation={deleteConversation}
            />
          ) : activeView === "gallery" ? (
            <GalleryHistory
              items={galleryHistory}
              refreshState={refreshFeedback.gallery}
              onRefresh={() => runRefresh("gallery", () => refreshGallery({ throwError: true }))}
              onDownload={downloadImage}
              onUseImage={useImageAsReference}
              onPreview={openImagePreview}
            />
          ) : activeView === "prompts" ? (
            <PromptLibrary
              items={prompts}
              draft={promptDraft}
              draftMode={promptDraftMode}
              filter={promptFilter}
              editingId={editingPromptId}
              copiedId={promptCopyId}
              onDraft={setPromptDraft}
              onDraftMode={setPromptDraftMode}
              onFilter={setPromptFilter}
              onSave={savePromptEntry}
              onCancel={() => { setPromptDraft(""); setPromptDraftMode(""); setEditingPromptId(null); }}
              onEdit={editPromptEntry}
              onDelete={deletePromptEntry}
              onCopy={copyPromptEntry}
              onUse={usePromptEntry}
              onFavorite={togglePromptFavorite}
              refreshState={refreshFeedback.prompts}
              onRefresh={() => runRefresh("prompts", () => refreshPrompts({ throwError: true }))}
            />
          ) : isSessionMode(form.mode) ? (
            <div className="chatPane" ref={scrollRef}>
              {messages.length === 0 && (
                <div className="emptyState">
                  {form.mode === "storyboard" ? <Clapperboard size={34} /> : form.mode === "chat" ? <Bot size={34} /> : form.mode === "edit" ? <Brush size={34} /> : <Wand2 size={34} />}
                  <h3>{form.mode === "storyboard" ? "描述一段视频想法" : form.mode === "chat" ? "把想法直接说出来" : form.mode === "edit" ? "在同一编辑会话里连续改图" : "在同一生图会话里连续提交完整提示词"}</h3>
                  <p>{form.mode === "storyboard" ? "AI 会先和你完善人物、场景与镜头，再按顺序用上一镜头画面继续 edit 生成下一张首帧。" : form.mode === "chat" ? "可以先生成，再上传上一张图继续改，动作选择 auto 时会自动判断。" : form.mode === "edit" ? "这里不会调用 AI 回复或扩写提示词，每次都会直接按你这次填写的完整要求编辑图片，但仍会保留在同一会话里。" : "这里不会调用 AI 回复或扩写提示词，每次都会直接按你这次填写的完整要求生图，但仍会保留在同一会话里。"}</p>
                </div>
              )}
              {messages.map((msg) => (
                <Message key={msg.id} msg={msg} onDownload={downloadImage} onPreview={openImagePreview} previewImages={chatGeneratedImages} />
              ))}
              {liveConversationTasks.map((task) => (
                <ChatTaskProgress key={task.id} task={task} />
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
            {["chat", "storyboard"].includes(form.mode) && (
              <>
                <ChatReferencePicker
                  images={chatGeneratedImages}
                  selected={chatReferenceImages}
                  onToggle={toggleChatReferenceImage}
                  onRemove={removeChatReferenceImage}
                  roles={chatReferenceRoles}
                  onRoleChange={updateSelectedReferenceRole}
                  uploadCount={chatImages.length}
                />
                <UploadRow
                  label={form.mode === "storyboard" ? "上传角色/场景参考" : "上传参考"}
                  files={chatImages}
                  onChange={updateChatUploads}
                  onRemove={(index) => {
                    const next = chatImages.filter((_, i) => i !== index);
                    setChatImages(next);
                    setChatUploadRoles((current) => normalizeUploadRoles(next, current));
                  }}
                  multiple
                  hint={`已指定 ${selectedReferenceCount()}/3 张`}
                  roles={chatUploadRoles}
                  onRoleChange={updateChatUploadRole}
                />
              </>
            )}
            {activeConversationLocked && (
              <div className="composerHint">
                当前{modeLabel(form.mode)}会话仍有任务在运行或排队。请先停止该会话任务，或点击“新任务”新开对话后再继续发送。
              </div>
            )}
            <div className="promptRow">
              <textarea
                value={form.prompt}
                onChange={(e) => setForm({ ...form, prompt: e.target.value })}
                onKeyDown={handlePromptKeyDown}
                placeholder={form.mode === "edit" ? "描述你想怎么改这张图..." : form.mode === "storyboard" ? "描述视频主题、人物、场景、镜头数量；也可以继续和 AI 讨论完善..." : "描述你想生成的画面..."}
              />
              <button className="sendButton" type="submit" disabled={submitDisabled}>
                {loading ? <Loader2 className="spin" size={20} /> : <Send size={20} />}
              </button>
            </div>
          </form>}
        </section>
      </section>
    </main>
    <ImagePreviewModal
      state={previewState}
      onClose={closeImagePreview}
      onMove={moveImagePreview}
      onDownload={downloadImage}
    />
    </>
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
  refreshState,
  onRefresh,
  onOpen,
  onOpenTask,
  onContinue,
  onSaveMeta,
  onSaveMessage,
  onDownload,
  onUseImage,
  onPreview,
  onCancelTask,
  onRetryTask,
  onDeleteTask,
  onDeleteConversation,
}) {
  const [draftTitle, setDraftTitle] = useState("");
  const [draftLimit, setDraftLimit] = useState(10);
  const [messageDrafts, setMessageDrafts] = useState({});
  const [modeFilter, setModeFilter] = useState("");
  const records = useMemo(() => buildHistoryRecords(conversations, tasks), [conversations, tasks]);
  const filteredRecords = useMemo(
    () => (!modeFilter ? records : records.filter((item) => item.mode === modeFilter)),
    [records, modeFilter],
  );
  const conversationTasks = useMemo(() => {
    if (!selected?.conversation) return [];
    const byId = new Map();
    for (const task of selected.tasks || []) byId.set(Number(task.id), task);
    for (const task of tasks || []) {
      if (Number(task.conversation_id) === Number(selected.conversation.id)) {
        byId.set(Number(task.id), { ...byId.get(Number(task.id)), ...task });
      }
    }
    return [...byId.values()].sort((a, b) => Number(b.id || 0) - Number(a.id || 0));
  }, [selected, tasks]);
  const conversationPreviewImages = useMemo(
    () => uniqueImages((selected?.messages || []).flatMap((msg) => msg.images || [])),
    [selected],
  );

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
          <div className="paneToolbarActions">
            <select value={modeFilter} onChange={(event) => setModeFilter(event.target.value)}>
              <option value="">全部模式</option>
              <option value="chat">对话</option>
              <option value="storyboard">分镜</option>
              <option value="generate">生成</option>
              <option value="edit">编辑</option>
            </select>
            <RefreshButton state={refreshState} onClick={onRefresh} />
          </div>
        </div>
        {filteredRecords.length === 0 ? (
          <div className="emptyMini">{records.length === 0 ? "暂无历史记录" : "当前筛选下没有历史记录"}</div>
        ) : filteredRecords.map((item) => (
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
            onPreview={onPreview}
            onContinue={onContinue}
            onDelete={onDeleteTask}
            onRetry={onRetryTask}
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
                <MessageCircle size={16} /> {continueLabel(resolveConversationMode(selected.conversation))}
              </button>
              <button className="ghostButton danger" type="button" onClick={() => onDeleteConversation(selected.conversation.id)}>
                <Trash2 size={16} /> 删除历史
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
                    <TaskMiniRow
                      key={task.id}
                      task={task}
                      onOpenTask={onOpenTask}
                      onCancelTask={onCancelTask}
                      onContinue={onContinue}
                      onRetryTask={onRetryTask}
                    />
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
                  {msg.image_error_detail && (
                    <InlineErrorBox title="这条回复的生图失败原因" error={msg.image_error_detail} />
                  )}
                  {msg.images?.length > 0 && (
                    <div className="imageGrid">
                      {msg.images.map((image, index) => (
                        <ImageCard
                          key={image.url}
                          image={image}
                          onDownload={onDownload}
                          onUseImage={onUseImage}
                          onPreview={() => {
                            const previewIndex = conversationPreviewImages.findIndex((item) => Number(item.id) === Number(image.id));
                            onPreview(conversationPreviewImages.length ? conversationPreviewImages : msg.images, previewIndex >= 0 ? previewIndex : index);
                          }}
                        />
                      ))}
                    </div>
                  )}
                  {msg.uploaded_images?.length > 0 && (
                    <div className="imageGrid uploadedImageGrid">
                      {msg.uploaded_images.map((image, index) => (
                        <ImageCard key={image.url} image={image} onDownload={onDownload} onPreview={() => onPreview(msg.uploaded_images, index)} />
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
    mode: item.mode || item.latest_task_mode || "chat",
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
      summary: taskProviderName(task) ? `${modeLabel(task.mode)}任务 #${task.id} · ${taskProviderName(task)}` : `${modeLabel(task.mode)}任务 #${task.id}`,
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
  const live = ["queued", "running"].includes(status);
  return (
    <span className={`statusPill ${status || "idle"}`}>
      {live && <Loader2 className="runningIcon spin" size={12} aria-hidden="true" />}
      <b>{modeLabel(mode)}</b>
      <em>{status ? statusLabel(status) : "记录"}</em>
    </span>
  );
}

function RunningTasksPanel({ tasks, open, onToggle, onOpenTask, onCancelTask, poolMeta }) {
  const firstTask = tasks[0];
  return (
    <section className={`runningPanel ${open ? "open" : ""}`} aria-live="polite">
      <button className="runningPanelHead" type="button" onClick={onToggle} aria-expanded={open}>
        <span>
          <Loader2 className="spin" size={15} aria-hidden="true" />
          <strong>{tasks.length} 个任务运行中</strong>
        </span>
        <small>{firstTask ? `${modeLabel(firstTask.mode)} #${firstTask.id} · ${taskProviderName(firstTask) || "等待分配提供商"}` : `生图池 ${poolMeta?.total_providers || 0} / 已用 ${poolMeta?.used_providers || 0} / 空闲 ${poolMeta?.idle_providers || 0}`}</small>
        <ChevronDown size={16} />
      </button>
      {open && (
        <div className="runningPanelBody">
          {tasks.map((task) => (
            <article className="runningPanelTask" key={task.id}>
              <div>
                <strong>{modeLabel(task.mode)}任务 #{task.id}</strong>
                <small>{task.stage || statusLabel(task.status)} · {Number(task.progress || 0)}%{taskProviderName(task) ? ` · ${taskProviderName(task)}` : ""}</small>
              </div>
              <div className="progressTrack">
                <div style={{ width: `${Math.max(4, Math.min(Number(task.progress || 0), 100))}%` }} />
              </div>
              <div className="runningPanelActions">
                <button type="button" onClick={() => onOpenTask(task.id)}>查看</button>
                <button type="button" onClick={() => onCancelTask(task.id)}>停止</button>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function TaskMiniRow({ task, onOpenTask, onCancelTask, onContinue, onRetryTask }) {
  const [open, setOpen] = useState(false);
  const isLive = ["queued", "running"].includes(task.status);
  const providerName = taskProviderName(task);
  const canRetry = ["generate", "edit", "storyboard"].includes(task.mode) && ["failed", "canceled"].includes(task.status);

  return (
    <article className={`taskMiniRow ${task.status} ${open ? "open" : ""}`}>
      <button className="taskMiniTitle" type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <span>
          {isLive && <Loader2 className="runningIcon spin" size={13} aria-hidden="true" />}
          <strong>{modeLabel(task.mode)}任务 #{task.id}</strong>
        </span>
        <small>{task.stage || statusLabel(task.status)} · {Number(task.progress || 0)}%{providerName ? ` · ${providerName}` : ""}</small>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="taskMiniBody">
          <div className="progressTrack">
            <div style={{ width: `${Math.max(4, Math.min(Number(task.progress || 0), 100))}%` }} />
          </div>
          <div className="taskMiniActions">
            <button type="button" onClick={() => onOpenTask(task.id)}>查看详情</button>
            {task.conversation_id && <button type="button" onClick={() => onContinue(task.conversation_id)}><MessageCircle size={14} /> {continueLabel(task.mode)}</button>}
            {canRetry && <button type="button" onClick={() => onRetryTask(task.id)}><RefreshCw size={14} /> 重试</button>}
            {isLive && <button className="danger" type="button" onClick={() => onCancelTask(task.id)}><X size={14} /> 停止</button>}
          </div>
          {task.error_detail && <ErrorSummaryBox title="失败原因" error={task.error_detail} className="taskMiniError" />}
        </div>
      )}
    </article>
  );
}

function TaskDetail({ task, onCancel, onDownload, onUseImage, onPreview, onContinue, onDelete, onRetry }) {
  const isLive = ["queued", "running"].includes(task.status);
  const images = normalizeTaskImages(task).filter((image) => image.source === "api" || !image.source);
  const inputImages = normalizeTaskImages(task).filter((image) => ["input", "mask", "input_reference"].includes(image.source));
  const providerName = taskProviderName(task);
  const canRetry = ["generate", "edit", "storyboard"].includes(task.mode) && ["failed", "canceled"].includes(task.status);

  return (
    <div className="taskDetail">
      <div className="taskDetailHead">
        <div>
          <StatusPill mode={task.mode} status={task.status} />
          <h3>{task.prompt || `${modeLabel(task.mode)}任务 #${task.id}`}</h3>
          <p>#{task.id} · {task.stage || statusLabel(task.status)} · 上游事件进度 {Number(task.progress || 0)}%</p>
        </div>
        <div className="taskDetailActions">
          {isLive && <button className="ghostButton danger" type="button" onClick={() => onCancel(task.id)}><X size={16} /> 停止</button>}
          {task.conversation_id && (
            <button className="secondaryButton compact" type="button" onClick={() => onContinue(task.conversation_id)}>
              <MessageCircle size={16} /> {continueLabel(task.mode)}
            </button>
          )}
          {canRetry && (
            <button className="secondaryButton compact" type="button" onClick={() => onRetry(task.id)}>
              <RefreshCw size={16} /> 重试原任务
            </button>
          )}
          <button className="ghostButton danger" type="button" onClick={() => onDelete(task.id)}>
            <Trash2 size={16} /> 删除历史
          </button>
        </div>
      </div>
      <div className="progressTrack large">
        <div style={{ width: `${Math.max(4, Math.min(Number(task.progress || 0), 100))}%` }} />
      </div>
      <div className="taskFacts">
        <span>创建：{formatTime(task.created_at)}</span>
        <span>更新：{formatTime(task.updated_at)}</span>
        <span>模式：{modeLabel(task.mode)}</span>
        {providerName && <span>生图提供商：{providerName}</span>}
      </div>
      {task.mode === "storyboard" && <StoryboardProgress task={task} />}
      {task.error_detail && <ErrorSummaryBox title="失败原因" error={task.error_detail} className="taskErrorBox" />}
      {inputImages.length > 0 && (
        <section>
          <div className="sectionTitle">
            <strong>任务输入图</strong>
            <small>{inputImages.length} 张</small>
          </div>
          <div className="imageGrid uploadedImageGrid">
            {inputImages.map((image, index) => <ImageCard key={image.id || image.url} image={image} onDownload={onDownload} onPreview={() => onPreview(inputImages, index)} />)}
          </div>
        </section>
      )}
      {images.length > 0 ? (
        <section>
          <div className="sectionTitle">
            <strong>生成图片</strong>
            <small>{images.length} 张</small>
          </div>
          <div className="imageGrid">
            {images.map((image, index) => <ImageCard key={image.id || image.url} image={image} onDownload={onDownload} onUseImage={onUseImage} onPreview={() => onPreview(images, index)} />)}
          </div>
        </section>
      ) : (
        <div className="emptyMini detailEmpty">这个任务还没有可查看的图片。</div>
      )}
    </div>
  );
}

function StoryboardProgress({ task = null, storyboard: storyboardProp = null, title = "分镜连续性", compact = false }) {
  const storyboard = storyboardProp || resolveStoryboardState(task);
  const shots = Array.isArray(storyboard.shots) ? storyboard.shots : [];
  if (!storyboard.character_summary && !storyboard.scene_summary && shots.length === 0) return null;
  const doneCount = shots.filter((shot) => shot.status === "done").length;
  const [expandedPrompts, setExpandedPrompts] = useState({});

  function togglePrompt(key) {
    setExpandedPrompts((current) => ({ ...current, [key]: !current[key] }));
  }

  return (
    <section className={`storyboardProgress ${compact ? "compact" : ""}`}>
      <div className="sectionTitle">
        <strong>{title}</strong>
        <small>{doneCount}/{shots.length || 0} 张</small>
      </div>
      <div className="storyboardBrief">
        <p><b>人物概述</b>{storyboard.character_summary || "AI 正在整理人物一致性信息"}</p>
        <p><b>场景概述</b>{storyboard.scene_summary || "AI 正在整理场景一致性信息"}</p>
      </div>
      {shots.length > 0 && (
        <div className="storyboardPromptSummary">
          <strong>本次计划生成 {shots.length} 张图</strong>
          <small>已先列出全部镜头提示词，随后会按顺序逐张实时生图。</small>
        </div>
      )}
      {shots.length > 0 && (
        <div className="shotTimeline">
          {shots.map((shot, index) => {
            const shotKey = `${shot.name || `镜头${index + 1}`}-${index}`;
            const prompt = storyboardShotPrompt(shot);
            const expanded = !!expandedPrompts[shotKey];
            return (
            <article className={`shotStep ${shot.status || "pending"}`} key={shotKey}>
              <span>{String(shot.order || index + 1).padStart(2, "0")}</span>
              <div>
                <strong>{shot.name || `镜头${index + 1}`}</strong>
                <small>{shot.status === "done" ? "已完成" : shot.status === "running" ? "生成中" : shot.status === "failed" ? "失败" : "等待中"}</small>
                {shot.continuity && <p>{shot.continuity}</p>}
                {prompt && (
                  <button
                    type="button"
                    className={`shotPromptToggle ${expanded ? "expanded" : ""}`}
                    onClick={() => togglePrompt(shotKey)}
                    aria-expanded={expanded}
                  >
                    <span className="shotPromptLabel">生图提示词</span>
                    <span className={`shotPromptText ${expanded ? "expanded" : ""}`}>{expanded ? prompt : storyboardPromptPreview(shot)}</span>
                    <em>{expanded ? "点击收起" : "点击展开全部内容"}</em>
                  </button>
                )}
              </div>
            </article>
          )})}
        </div>
      )}
    </section>
  );
}

function PromptLibrary({
  items,
  draft,
  draftMode,
  filter,
  editingId,
  copiedId,
  refreshState,
  onDraft,
  onDraftMode,
  onFilter,
  onSave,
  onCancel,
  onEdit,
  onDelete,
  onCopy,
  onUse,
  onFavorite,
  onRefresh,
}) {
  return (
    <div className="promptLibrary">
      <section className="promptEditor">
        <div className="paneToolbar">
          <strong>{editingId ? "修改提示词" : "新增提示词"}</strong>
          <RefreshButton state={refreshState} onClick={onRefresh} />
        </div>
        <textarea
          value={draft}
          onChange={(event) => onDraft(event.target.value)}
          placeholder="写入一条常用提示词，只保存文字，不保存图片。"
        />
        <Select
          label="提示词模式"
          value={draftMode}
          onChange={onDraftMode}
          options={[
            { value: "", label: "通用" },
            { value: "chat", label: "对话" },
            { value: "storyboard", label: "分镜" },
            { value: "generate", label: "生成" },
            { value: "edit", label: "编辑" },
          ]}
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

      <section className="promptFilters">
        <label>
          <Search size={15} />
          <input value={filter.q} onChange={(event) => onFilter({ ...filter, q: event.target.value })} placeholder="搜索提示词" />
        </label>
        <select value={filter.mode} onChange={(event) => onFilter({ ...filter, mode: event.target.value })}>
          <option value="">全部模式</option>
          <option value="chat">对话</option>
          <option value="storyboard">分镜</option>
          <option value="generate">生成</option>
          <option value="edit">编辑</option>
        </select>
        <button className={filter.favorite ? "active" : ""} type="button" onClick={() => onFilter({ ...filter, favorite: !filter.favorite })}>
          <Heart size={15} /> 收藏
        </button>
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
                <button className={item.favorite ? "favorite active" : "favorite"} type="button" onClick={() => onFavorite(item)}>
                  <Heart size={15} /> {item.favorite ? "已收藏" : "收藏"}
                </button>
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

function GalleryHistory({ items, refreshState, onRefresh, onDownload, onUseImage, onPreview }) {
  const groups = useMemo(() => groupImages(items), [items]);
  const [expandedGroups, setExpandedGroups] = useState({});
  const [modeFilter, setModeFilter] = useState("");
  const filteredGroups = useMemo(
    () => (!modeFilter ? groups : groups.filter((group) => group.mode === modeFilter)),
    [groups, modeFilter],
  );

  useEffect(() => {
    setExpandedGroups((current) => {
      const next = {};
      for (const group of filteredGroups) {
        if (current[group.key]) next[group.key] = true;
      }
      return next;
    });
  }, [filteredGroups]);

  function toggleGroup(groupKey) {
    setExpandedGroups((current) => ({ ...current, [groupKey]: !current[groupKey] }));
  }

  return (
    <div className="galleryHistory">
      <div className="paneToolbar">
        <strong>历史图库</strong>
        <div className="paneToolbarActions">
          <select value={modeFilter} onChange={(event) => setModeFilter(event.target.value)}>
            <option value="">全部模式</option>
            <option value="chat">对话</option>
            <option value="storyboard">分镜</option>
            <option value="generate">生成</option>
            <option value="edit">编辑</option>
          </select>
          <RefreshButton state={refreshState} onClick={onRefresh} />
        </div>
      </div>
      {filteredGroups.length === 0 ? (
        <div className="emptyState">
          <Images size={34} />
          <h3>{groups.length === 0 ? "还没有历史图片" : "当前筛选下没有图片"}</h3>
          <p>这里会按会话保存生成图、继续改输入图和参考图，并支持按模式筛选。</p>
        </div>
      ) : filteredGroups.map((group) => {
        const expanded = !!expandedGroups[group.key];
        const visibleItems = expanded ? group.items : group.preview_items;
        return (
        <article className="galleryGroup" key={group.key}>
          <div className="galleryGroupHead">
            <div className="galleryGroupMeta">
              <span>{group.title}</span>
              <small>{group.mode ? modeLabel(group.mode) : "未分类"} · {group.time} · 共 {group.items.length} 张</small>
            </div>
            {group.has_more && (
              <button type="button" className="galleryGroupToggle" onClick={() => toggleGroup(group.key)}>
                {expanded ? "收起" : `查看详情（全部 ${group.items.length} 张）`}
              </button>
            )}
          </div>
          <div className="imageGrid">
            {visibleItems.map((image) => {
              const previewIndex = group.items.findIndex((item) => Number(item.id) === Number(image.id));
              return (
                <ImageCard
                  key={image.id}
                  image={image}
                  onDownload={onDownload}
                  onUseImage={onUseImage}
                  onPreview={() => onPreview(group.items, previewIndex >= 0 ? previewIndex : 0)}
                />
              );
            })}
          </div>
          {!expanded && group.has_more && (
            <small className="galleryGroupHint">当前先展示 {group.preview_items.length} 张预览图，点击“查看详情”可查看该会话全部图片。</small>
          )}
        </article>
      )})}
    </div>
  );
}

function ChatTaskProgress({ task }) {
  const providerName = taskProviderName(task);
  return (
    <div className="message assistant taskMessage">
      <div className="avatar"><Loader2 className="spin" size={18} /></div>
      <div className="bubble taskBubble">
        <div className="taskBubbleHead">
          <strong>{task.stage || statusLabel(task.status)}</strong>
          <span>{Number(task.progress || 0)}%</span>
        </div>
        <div className="progressTrack">
          <div style={{ width: `${Math.max(4, Math.min(Number(task.progress || 0), 100))}%` }} />
        </div>
        <small>#{task.id} · {task.params?.action || task.mode} · 上游事件会实时写入这里</small>
        {providerName && <small>当前生图提供商：{providerName}</small>}
        {task.mode === "storyboard" && <StoryboardProgress task={task} title="本次分镜计划" compact />}
      </div>
    </div>
  );
}

function resolveStoryboardState(source) {
  if (!source || typeof source !== "object") return {};
  if (source.params?.storyboard && typeof source.params.storyboard === "object") return source.params.storyboard;
  if (source.response?.raw?.storyboard && typeof source.response.raw.storyboard === "object") return source.response.raw.storyboard;
  if (source.response?.raw?.plan && typeof source.response.raw.plan === "object") return source.response.raw.plan;
  if (source.storyboard && typeof source.storyboard === "object") return source.storyboard;
  if (source.plan && typeof source.plan === "object") return source.plan;
  return {};
}

function storyboardShotPrompt(shot) {
  return String(shot?.execution_prompt || shot?.planner_prompt || shot?.prompt || "").trim();
}

function storyboardPromptPreview(shot) {
  const singleLine = storyboardShotPrompt(shot).replace(/\s+/g, " ").trim();
  return singleLine.length > 120 ? `${singleLine.slice(0, 120)}...` : singleLine;
}

function ChatReferencePicker({ images, selected, onToggle, onRemove, roles = {}, onRoleChange, uploadCount = 0 }) {
  const selectedIds = new Set((selected || []).map((image) => Number(image.id)));
  const selectedImages = uniqueImages(selected || []);
  return (
    <section className="referencePicker">
      <div className="referencePickerHead">
        <strong>指定参考图</strong>
        <small>{selected.length}/3，建议明确区分角色、场景和道具锚点</small>
      </div>
      {selectedImages.length > 0 && (
        <div className="selectedReferenceStrip">
          {selectedImages.map((image, index) => {
            const role = roles[String(image.id)] || defaultReferenceRole(uploadCount + index);
            return (
              <div className="selectedReferenceCard" key={image.id || image.url}>
                <img src={image.public_url || image.url} alt="" />
                <div className="selectedReferenceMeta">
                  <strong>{image.title || image.filename || `参考图 ${index + 1}`}</strong>
                  <small>{referenceRoleLabel(role)}</small>
                </div>
                <label className="referenceRoleSelect compact">
                  <span>用途</span>
                  <select value={role} onChange={(event) => onRoleChange?.(image.id, event.target.value)}>
                    {referenceRoleOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                  </select>
                </label>
                <button type="button" className="selectedReferenceRemove" onClick={() => onRemove?.(image.id)}>
                  <X size={14} />
                  移除
                </button>
              </div>
            );
          })}
        </div>
      )}
      {images.length > 0 ? (
        <div className="referenceStrip">
          {images.map((image, index) => {
            const isSelected = selectedIds.has(Number(image.id));
            const role = roles[String(image.id)] || defaultReferenceRole(uploadCount + index);
            return (
              <div className={`referenceChoice ${isSelected ? "active" : ""}`} key={image.id}>
                <button
                  type="button"
                  className={isSelected ? "active" : ""}
                  onClick={() => onToggle(image)}
                  title={isSelected ? "取消选择" : "选择为本轮参考图"}
                >
                  <img src={image.public_url || image.url} alt="" />
                  <span>{isSelected ? "已选" : "选择"}</span>
                  {isSelected && <em>取消</em>}
                </button>
                {isSelected && (
                  <label className="referenceRoleSelect">
                    <small>作为{referenceRoleLabel(role)}使用</small>
                    <select value={role} onChange={(event) => onRoleChange?.(image.id, event.target.value)}>
                      {referenceRoleOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <small className="uploadEmpty">{selectedImages.length > 0 ? "当前图已加入本轮参考区，可以继续补充上传其它参考图。" : "当前对话暂无可选历史图，也可以直接上传参考图。"}</small>
      )}
    </section>
  );
}

function Message({ msg, onDownload, onPreview, previewImages = [] }) {
  const storyboard = resolveStoryboardState(msg?.meta);
  return (
    <div className={`message ${msg.role}`}>
      <div className="avatar">{msg.role === "user" ? "你" : <Bot size={18} />}</div>
      <div className="bubble">
        <p>{msg.content}</p>
        {msg.role === "assistant" && <StoryboardProgress storyboard={storyboard} title="本次分镜计划" compact />}
        {msg.image_error_detail && (
          <InlineErrorBox title="生图失败原因" error={msg.image_error_detail} />
        )}
        {msg.previews?.length > 0 && (
          <div className="imageGrid">
            {msg.previews.map((url) => <img key={url} src={url} alt="" />)}
          </div>
        )}
        {msg.uploaded_images?.length > 0 && (
          <div className="imageGrid uploadedImageGrid">
            {msg.uploaded_images.map((image, index) => <ImageCard key={image.url} image={image} onDownload={onDownload} onPreview={() => onPreview(msg.uploaded_images, index)} />)}
          </div>
        )}
        {msg.images?.length > 0 && (
          <div className="imageGrid">
            {msg.images.map((image, index) => {
              const previewIndex = previewImages.findIndex((item) => Number(item.id) === Number(image.id));
              return <ImageCard key={image.url} image={image} onDownload={onDownload} onPreview={() => onPreview(previewImages.length ? previewImages : msg.images, previewIndex >= 0 ? previewIndex : index)} />;
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function ImagePreviewModal({ state, onClose, onMove, onDownload }) {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [drag, setDrag] = useState(null);
  const stageRef = useRef(null);
  const image = state?.items?.[state.index];
  const url = image?.public_url || image?.url;

  useEffect(() => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
    setDrag(null);
  }, [state?.index, url]);

  useEffect(() => {
    if (!state) return undefined;
    function onKey(event) {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft") onMove(-1);
      if (event.key === "ArrowRight") onMove(1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state, onClose, onMove]);

  useEffect(() => {
    const stage = stageRef.current;
    if (!state || !stage) return undefined;
    function handleWheel(event) {
      event.preventDefault();
      const direction = event.deltaY > 0 ? -0.12 : 0.12;
      setZoom((value) => Math.max(0.25, Math.min(6, Number((value + direction).toFixed(2)))));
    }
    stage.addEventListener("wheel", handleWheel, { passive: false });
    return () => stage.removeEventListener("wheel", handleWheel);
  }, [state, url]);

  if (!state || !image || !url) return null;

  function startDrag(event) {
    event.preventDefault();
    setDrag({ x: event.clientX, y: event.clientY, baseX: offset.x, baseY: offset.y });
  }

  function moveDrag(event) {
    if (!drag) return;
    setOffset({
      x: drag.baseX + event.clientX - drag.x,
      y: drag.baseY + event.clientY - drag.y,
    });
  }

  const title = image.title || image.filename || `图片 ${state.index + 1}`;

  return (
    <div className="previewOverlay" role="dialog" aria-modal="true" onMouseMove={moveDrag} onMouseUp={() => setDrag(null)} onMouseLeave={() => setDrag(null)}>
      <div className="previewPanel">
        <div className="previewHeader">
          <div>
            <strong title={title}>{title}</strong>
            <small>{state.index + 1}/{state.items.length} · 滚轮缩放，拖拽移动，方向键切换</small>
          </div>
          <div className="previewHeaderActions">
            <button type="button" onClick={() => setZoom((value) => Math.max(0.25, Number((value - 0.2).toFixed(2))))}>缩小</button>
            <button type="button" onClick={() => { setZoom(1); setOffset({ x: 0, y: 0 }); }}>原始适配</button>
            <button type="button" onClick={() => setZoom((value) => Math.min(6, Number((value + 0.2).toFixed(2))))}>放大</button>
            <button type="button" onClick={() => onDownload(image)}><Download size={14} /> 下载</button>
            <button type="button" onClick={onClose} title="关闭"><X size={16} /></button>
          </div>
        </div>
        <div className="previewStage" ref={stageRef}>
          {state.items.length > 1 && (
            <button className="previewNav prev" type="button" onClick={() => onMove(-1)} title="上一张">
              <ChevronLeft size={28} />
            </button>
          )}
          <img
            src={url}
            alt={title}
            draggable={false}
            onMouseDown={startDrag}
            style={{
              transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`,
              cursor: drag ? "grabbing" : "grab",
            }}
          />
          {state.items.length > 1 && (
            <button className="previewNav next" type="button" onClick={() => onMove(1)} title="下一张">
              <ChevronRight size={28} />
            </button>
          )}
        </div>
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

function ImageCard({ image, onDownload, onUseImage, onPreview }) {
  const [showPrompt, setShowPrompt] = useState(false);
  const url = image.public_url || image.url;
  const promptText = image.prompt_text || image.task_prompt || image.message_content || "";
  return (
    <div className="imageCard">
      <img src={url} alt="generated" />
      <div className="imageActions">
        <button type="button" onClick={onPreview || (() => window.open(url, "_blank", "noreferrer"))}>
          <ExternalLink size={14} />
          预览
        </button>
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
        {promptText && (
          <button type="button" onClick={() => setShowPrompt((value) => !value)}>
            <BookOpen size={14} />
            提示词
          </button>
        )}
      </div>
      {showPrompt && (
        <div className="imagePromptBox">
          <strong>原始提示词</strong>
          <p>{promptText}</p>
        </div>
      )}
    </div>
  );
}

function RefreshButton({ state = "idle", onClick }) {
  const busy = state === "loading";
  const Icon = state === "success" ? Check : state === "failed" ? X : RefreshCw;
  const label = state === "loading" ? "刷新中" : state === "success" ? "已刷新" : state === "failed" ? "刷新失败" : "刷新";
  return (
    <button
      type="button"
      className={`refreshButton ${state}`}
      onClick={onClick}
      disabled={busy}
      aria-live="polite"
    >
      <Icon className={busy ? "spin" : ""} size={15} />
      {label}
    </button>
  );
}

function ErrorSummaryBox({ title = "失败原因", error, className = "" }) {
  const [open, setOpen] = useState(false);
  const [copyState, setCopyState] = useState("idle");
  const errorText = formatTaskError(error);
  const summaryText = errorSummaryText(error);

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
    <section className={`inlineErrorBox ${open ? "open" : ""} ${className}`.trim()}>
      <button type="button" className="errorSummaryToggle" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <div>
          <strong>{title}</strong>
          <span>{summaryText}</span>
        </div>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="errorDetailBody">
          <div className="sectionTitle">
            <small>详细信息</small>
            <button className={`copyFeedbackButton ${copyState}`} type="button" onClick={copyError} disabled={copyState === "copying"}>
              <Copy size={14} /> {copyState === "copying" ? "复制中" : copyState === "success" ? "复制成功" : copyState === "failed" ? "复制失败" : "复制原因"}
            </button>
          </div>
          <pre>{errorText}</pre>
        </div>
      )}
    </section>
  );
}

function InlineErrorBox({ title = "失败原因", error }) {
  return <ErrorSummaryBox title={title} error={error} />;
}

function SettingsSaveAction({ feedback, label, onClick }) {
  const busy = feedback?.state === "loading";
  return (
    <div className="settingsSaveAction">
      {feedback?.message && <div className={`settingsSaveNotice ${feedback.state || "idle"}`}>{feedback.message}</div>}
      <button className="secondaryButton compact" type="button" onClick={onClick} disabled={busy}>
        <Check size={16} /> {busy ? "保存中" : label}
      </button>
    </div>
  );
}

function Field({ label, children, help = "", className = "" }) {
  return (
    <label className={`field ${className}`.trim()}>
      <span className="fieldLabel" title={help || undefined}>{label}</span>
      {children}
    </label>
  );
}

function Select({ label, value, onChange, options, help = "", className = "" }) {
  return (
    <Field label={label} help={help} className={className}>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => {
          const normalized = typeof option === "string" ? { value: option, label: option } : option;
          return <option key={normalized.value} value={normalized.value}>{normalized.label}</option>;
        })}
      </select>
    </Field>
  );
}

function UploadRow({ label, files, onChange, onRemove, multiple = false, hint = "", roles = {}, onRoleChange }) {
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
        {hint && <small>{hint}</small>}
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
            {onRoleChange && (
              <label className="referenceRoleSelect compact">
                <span>{referenceRoleLabel(roles[uploadFileRoleKey(item.file, index)] || defaultReferenceRole(index))}</span>
                <select
                  value={roles[uploadFileRoleKey(item.file, index)] || defaultReferenceRole(index)}
                  onChange={(event) => onRoleChange(item.file, index, event.target.value)}
                >
                  {referenceRoleOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
              </label>
            )}
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

function errorSummaryText(error) {
  const detail = error?.detail ?? error ?? {};
  const summary = typeof detail === "string"
    ? detail
    : detail?.message || detail?.upstream?.message || detail?.fallback_error?.upstream?.message || detail?.raw || JSON.stringify(compactErrorForDisplay(detail));
  const singleLine = compactString(String(summary || "请求失败")).replace(/\s+/g, " ").trim();
  return singleLine.length > 120 ? `${singleLine.slice(0, 120)}...` : singleLine;
}

function formatTaskError(error) {
  return typeof error === "string"
    ? compactString(error)
    : JSON.stringify(compactErrorForDisplay(error), null, 2);
}

function parseJsonObject(value) {
  if (!value || typeof value !== "string") return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
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

function normalizeImageForClient(image) {
  return {
    ...image,
    url: image.public_url || image.url,
    public_url: image.public_url || image.url,
    filename: image.file_path?.split(/[\\/]/).pop() || image.filename || "generated-image.png",
  };
}

function uniqueImages(images) {
  const seen = new Set();
  const result = [];
  for (const image of images || []) {
    const id = image.id || image.url;
    if (!id || seen.has(id)) continue;
    seen.add(id);
    result.push(normalizeImageForClient(image));
  }
  return result;
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
    const conversationKey = item.conversation_id ? `conversation-${item.conversation_id}` : null;
    const fallbackKey = item.task_id ? `task-${item.task_id}` : `image-${item.id || item.created_at || Math.random()}`;
    const key = conversationKey || fallbackKey;
    const title = item.conversation_title || item.title || item.task_prompt || "独立生成";
    const mode = item.task_mode || "";
    if (!map.has(key)) {
      map.set(key, {
        key,
        title,
        items: [],
        latest_time: item.created_at || "",
        mode,
      });
    }
    const group = map.get(key);
    group.items.push(item);
    if (!group.mode && mode) group.mode = mode;
    const currentStamp = new Date(item.created_at || 0).getTime();
    const latestStamp = new Date(group.latest_time || 0).getTime();
    if (Number.isFinite(currentStamp) && currentStamp >= latestStamp) {
      group.latest_time = item.created_at || group.latest_time;
    }
  }
  return [...map.values()]
    .map((group) => {
      const sortedItems = [...group.items].sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));
      const previewItems = sortedItems.slice(0, 6);
      return {
        ...group,
        items: sortedItems,
        preview_items: previewItems,
        has_more: sortedItems.length > previewItems.length,
        time: group.latest_time ? new Date(group.latest_time).toLocaleString() : "未知时间",
      };
    })
    .sort((a, b) => new Date(b.latest_time || 0) - new Date(a.latest_time || 0));
}

createRoot(document.getElementById("root")).render(<App />);
