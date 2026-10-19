// 全局配置
window.APP_CONFIG = window.APP_CONFIG || {
    apiBaseUrl: ''
};

// 实时卡片渲染偏好只影响叮当前端展示，不改变 ACPs/AIP 原始事件与调用内容。
const AGENT_RENDERING_STORAGE_KEY = 'dingdang.realtimeAgentCardRendering';
const UI_HELPERS = window.DingdangUiHelpers || {};
const MESSAGE_RESOURCES = window.DingdangMessageResources || {};
const MESSAGE_INPUT_MAX_HEIGHT = 150;
const THEME_STORAGE_KEY = 'dingdang.theme';
const THEME_PREFERENCES = new Set(['dark', 'light', 'system']);
let systemThemeQuery = null;
let mobileSidebarQuery = null;
let themeControlsInitialized = false;
let shellControlsInitialized = false;
let sidebarAccountControlsInitialized = false;
let dynamicActionDelegationInitialized = false;
let dynamicActionSequence = 0;
const dynamicActionPayloads = new Map();
const DYNAMIC_ACTION_PAYLOAD_LIMIT = 512;

function registerDynamicActionPayload(payload) {
    while (dynamicActionPayloads.size >= DYNAMIC_ACTION_PAYLOAD_LIMIT) {
        dynamicActionPayloads.delete(dynamicActionPayloads.keys().next().value);
    }
    const key = `ui-action-${++dynamicActionSequence}`;
    dynamicActionPayloads.set(key, payload);
    return key;
}

function releaseDynamicActionPayloads(root) {
    if (!root) return;
    const nodes = [];
    if (root.dataset?.actionPayload) nodes.push(root);
    root.querySelectorAll?.('[data-action-payload]').forEach(node => nodes.push(node));
    nodes.forEach(node => dynamicActionPayloads.delete(node.dataset.actionPayload));
}

function replaceDynamicActionHTML(element, html) {
    if (!element) return;
    releaseDynamicActionPayloads(element);
    element.innerHTML = html;
}

function clearDynamicActionElement(element) {
    replaceDynamicActionHTML(element, '');
}

function removeDynamicActionElement(element) {
    if (!element) return;
    releaseDynamicActionPayloads(element);
    element.remove();
}

function renderCopyButton(text) {
    const key = registerDynamicActionPayload({ action: 'copy-message', text: String(text || '') });
    return `<button class="copy-btn" type="button" data-ui-action="copy-message" data-action-payload="${escapeHTMLAttribute(key)}" aria-label="复制消息">⎘</button>`;
}

function renderDynamicActionAttributes(action, payload) {
    const key = registerDynamicActionPayload({ action, ...payload });
    return `data-ui-action="${escapeHTMLAttribute(action)}" data-action-payload="${escapeHTMLAttribute(key)}"`;
}

function getSystemThemeQuery() {
    if (!systemThemeQuery && typeof window.matchMedia === 'function') {
        systemThemeQuery = window.matchMedia('(prefers-color-scheme: dark)');
    }
    return systemThemeQuery;
}

function normalizeThemePreference(preference) {
    const normalized = String(preference || '').toLowerCase();
    return THEME_PREFERENCES.has(normalized) ? normalized : 'system';
}

function applyThemePreference(preference) {
    const normalized = normalizeThemePreference(preference);
    const prefersDark = Boolean(getSystemThemeQuery()?.matches);
    const effectiveTheme = normalized === 'system' ? (prefersDark ? 'dark' : 'light') : normalized;
    const root = document.documentElement;
    root.dataset.theme = normalized;
    root.dataset.effectiveTheme = effectiveTheme;
    root.style.colorScheme = effectiveTheme;

    const labels = { dark: '深色', light: '浅色', system: '跟随系统' };
    document.querySelectorAll('[data-theme-value]').forEach(button => {
        button.setAttribute('aria-checked', String(button.dataset.themeValue === normalized));
    });
    const menuButton = document.getElementById('themeMenuButton');
    if (menuButton) menuButton.setAttribute('aria-label', `选择主题，当前${labels[normalized]}`);
    return normalized;
}

function setThemePreference(preference) {
    const normalized = applyThemePreference(preference);
    try {
        localStorage.setItem(THEME_STORAGE_KEY, normalized);
    } catch (error) {
        console.warn('[Dingdang] 保存主题偏好失败:', error);
    }
    return normalized;
}

function initializeTheme() {
    let savedPreference = 'dark';
    try {
        savedPreference = localStorage.getItem(THEME_STORAGE_KEY) || 'dark';
    } catch (error) {
        console.warn('[Dingdang] 读取主题偏好失败，使用深色主题:', error);
    }
    const preference = applyThemePreference(savedPreference);
    const query = getSystemThemeQuery();
    if (query && !query.__dingdangThemeListenerAttached) {
        query.addEventListener?.('change', () => {
            if (document.documentElement.dataset.theme === 'system') applyThemePreference('system');
        });
        query.__dingdangThemeListenerAttached = true;
    }
    return preference;
}

// 从本地存储读取渲染模式；默认开启，保证已有演示流程不被静默降级。
function readRealtimeAgentRenderingPreference() {
    try {
        return localStorage.getItem(AGENT_RENDERING_STORAGE_KEY) !== 'off';
    } catch (error) {
        console.warn('[Dingdang] 读取实时卡片渲染偏好失败，使用默认开启:', error);
        return true;
    }
}

// 状态管理
const state = {
    user: null,
    authProvider: 'sqlite',
    currentConversationId: null,
    conversations: [],
    conversationRuntimes: Object.create(null),
    historyDeleteConfirmationId: null,
    messages: [],
    isLoading: false,
    activeRequestController: null,
    activeExecutionInteraction: null,
    activeExecutionAgentCount: 0,
    currentWebAgents: [],
    currentIoaAgents: [],
    selectedIoaAgents: new Set(),
    agentDiscoveryPanels: {},
    agentDiscoveryPanelSequence: 0,
    activeAgentDiscoveryPanelId: '',
    weatherAgentApp: {
        active: false,
        launchTimer: null,
        lastCardDomId: '',
        currentPresetId: '',
        continuingByCard: {},
        pages: {},
        order: [],
        activePageKey: ''
    },
    currentTaskIntent: {
        weather: false,
        queryText: '',
        preferredPresetId: '',
        attachmentIds: []
    },
    adminUsers: [],
    adminUserSearchKeyword: '',
    adminUserPage: 1,
    adminUserPageSize: 10,
    adminUserTotalPages: 1,
    adminUserTotal: 0,
    adminLogs: [],
    adminLogsPage: 1,
    adminLogsPageSize: 20,
    adminLogsTotalPages: 1,
    adminLogsTotal: 0,
    adminLogsFilters: {
        user_id: '',
        username: '',
        event_type: '',
        start_time: '',
        end_time: '',
        keyword: ''
    },
    currentProcessCard: null,
    agentResultCards: {},
    agentResultEvents: {},
    activeAgentInteraction: null,
    pendingSecretaryCollaboration: null,
    pendingSecretaryCompletion: null,
    pendingAgentCompletion: null,
    agentInteractionHistory: {},
    agentInteractionState: {},
    rawAgentResultCopyTexts: {},
    hasAgentResultCardsInCurrentRun: false,
    hasRawAgentResultsInCurrentRun: false,
    workflowHandoffs: {},
    collaborationRuns: Object.create(null),
    health: { timer: null, refreshing: false },
    realtimeAgentCardRendering: false,
    currentImage: { file: null, previewUrl: null, imageUrl: null, artifactId: null, uploading: false },
    messageResources: {},
    messageResourceSequence: 0,
    messageImageLightboxTrigger: null,
    currentAttachments: []
};

function conversationRuntimeKey(conversationId) {
    return conversationId === null || conversationId === undefined || conversationId === ''
        ? ''
        : String(conversationId);
}

function getConversationRuntime(conversationId) {
    const key = conversationRuntimeKey(conversationId);
    if (!key) return { activity: 'idle', loading: false, controller: null };
    if (!state.conversationRuntimes[key]) {
        state.conversationRuntimes[key] = { activity: 'idle', loading: false, controller: null };
    }
    return state.conversationRuntimes[key];
}

function bindConversationRuntime(runtimeKey, conversationId) {
    const targetKey = conversationRuntimeKey(conversationId);
    if (!targetKey || runtimeKey === targetKey) return getConversationRuntime(targetKey);
    const runtime = state.conversationRuntimes[runtimeKey] || getConversationRuntime(runtimeKey);
    state.conversationRuntimes[targetKey] = runtime;
    delete state.conversationRuntimes[runtimeKey];
    return runtime;
}

function setConversationActivity(conversationId, activity) {
    const runtime = getConversationRuntime(conversationId);
    runtime.activity = activity || 'idle';
    const item = state.conversations.find(session => String(session.id) === String(conversationId));
    if (item) item.activity_state = runtime.activity;
    renderConversationHistory();
}

function updateConversationTitle(conversationId, title) {
    const normalizedTitle = String(title || '').trim();
    if (!conversationId || !normalizedTitle) return;
    const item = state.conversations.find(session => String(session.id) === String(conversationId));
    if (!item) {
        void loadConversationHistory();
        return;
    }
    item.title = normalizedTitle;
    renderConversationHistory();
}

function setConversationLoading(conversationId, loading, controller = null) {
    const runtime = getConversationRuntime(conversationId);
    runtime.loading = Boolean(loading);
    runtime.controller = loading ? controller : null;
    setConversationActivity(conversationId, loading ? 'running' : runtime.activity);
    if (String(conversationId) === String(state.currentConversationId) || (!state.currentConversationId && String(conversationId).startsWith('draft-'))) {
        state.isLoading = runtime.loading;
        state.activeRequestController = runtime.controller;
        setComposerTaskRunning(runtime.loading);
    }
}

// 续办期间也保留可取消目标：等待提示会立即撤下，但停止键仍须能取消同一个 AIP 任务或协作 run。
function beginAgentContinuationExecution(interaction) {
    const conversationId = interaction?.conversation_id || state.currentConversationId;
    const controller = new AbortController();
    state.activeExecutionInteraction = interaction || null;
    setConversationLoading(conversationId, true, controller);
    return { conversationId, controller };
}

function finishAgentContinuationExecution(conversationId, controller) {
    const runtime = getConversationRuntime(conversationId);
    if (runtime.controller === controller) {
        setConversationLoading(conversationId, false);
    }
    state.activeExecutionInteraction = null;
}

// 从执行 SSE 及时记住可取消目标，使首次调用与多智能体协作同样能由停止键结束。
function rememberActiveExecutionInteraction(event) {
    if (!event || !state.isLoading) return;
    if (event.run_id && String(event.type || '').startsWith('collaboration_')) {
        const lifecycleState = String(event.status || event.state || '').toLowerCase();
        const aipTaskId = event.aip_task_id || event.aipTaskId || '';
        const previous = state.activeExecutionInteraction?.interaction_kind === 'collaboration'
            && state.activeExecutionInteraction.run_id === event.run_id
            ? state.activeExecutionInteraction
            : null;
        const collaborationTasks = Array.isArray(previous?.collaboration_tasks)
            ? [...previous.collaboration_tasks]
            : [];
        if (aipTaskId) {
            const descriptor = {
                task_id: aipTaskId,
                agent: event.agent_name || event.agent || '协作成员',
                agent_name: event.agent_name || event.agent || '协作成员',
                endpoint: event.endpoint || '',
                aic: event.aic || event.agent_aic || '',
                session_id: event.session_id || event.sessionId || ''
            };
            const existingIndex = collaborationTasks.findIndex(item => item.task_id === aipTaskId);
            if (event.type === 'collaboration_task'
                && ['completed', 'failed', 'canceled', 'cancelled'].includes(lifecycleState)) {
                if (existingIndex >= 0) collaborationTasks.splice(existingIndex, 1);
            } else if (existingIndex >= 0) {
                collaborationTasks[existingIndex] = { ...collaborationTasks[existingIndex], ...descriptor };
            } else {
                collaborationTasks.push(descriptor);
            }
        }
        const currentTask = collaborationTasks[0]?.task_id || '';
        state.activeExecutionInteraction = {
            ...(previous || {}),
            ...event,
            interaction_kind: 'collaboration',
            conversation_id: event.conversation_id || state.currentConversationId,
            run_id: event.run_id,
            task_id: currentTask,
            collaboration_tasks: collaborationTasks,
            agent: event.agent_name || event.agent || '叮当',
            agent_name: event.agent_name || event.agent || '叮当'
        };
        return;
    }
    if (state.activeExecutionInteraction?.interaction_kind === 'collaboration') return;
    if (event.task_id || event.taskId) {
        state.activeExecutionInteraction = { ...event, conversation_id: event.conversation_id || state.currentConversationId };
    }
}

// API 配置 - 自动检测页面路径前缀
function getApiBase() {
    if (window.APP_CONFIG.apiBaseUrl) {
        return window.APP_CONFIG.apiBaseUrl + '/api';
    }
    var pathname = window.location.pathname;
    if (pathname && pathname !== '/' && !pathname.endsWith('/') && !pathname.includes('.')) {
        return pathname + '/api';
    }
    var lastSlash = pathname.lastIndexOf('/');
    var basePath = lastSlash > 0 ? pathname.substring(0, lastSlash) : '';
    return basePath + '/api';
}
const API_BASE = getApiBase();

// 能力启动弹窗最短展示时间；给演示观众足够时间看清“发现智能体 -> AIP 调用 -> 打开应用”的过程。
const CAPABILITY_LAUNCH_MIN_VISIBLE_MS = 2400;

// 顺序演示中，天气完成卡必须留在屏幕上供观众阅读；该等待只影响前端换页，
// 不延迟结果落库，也不计入后续工作流的执行超时。
const WEATHER_HANDOFF_PRESENTATION_HOLD_MS = 10000;

// 城市图册资源版本：用于刷新浏览器/WebView 的城市背景缓存，避免继续使用旧 SVG 或未接入真实图册的资源。
const WEATHER_CITY_ASSET_VERSION = '20260709-weather-district-redraw-v2';

// Agent 应用预设注册表：只管理叮当前端如何展示 AIP 返回，不改变 ACPs/AIP/ACS 协议结构。
const AGENT_APP_PRESETS = [
    {
        id: 'weather.summary.v1',
        kind: 'weather',
        title: '天气应用',
        shortTitle: '天气',
        kicker: 'A2UI / MC Weather App',
        launchTitle: '已找到天气智能体',
        launchVerb: '正在启动天气应用',
        statusLabel: '已启用',
        supportLabel: '专用 A2UI 天气模板',
        keywords: /天气|气温|温度|降水|降雨|下雨|湿度|风速|weather|temperature|rain/i,
        matcher: (event, content) => isWeatherAgentCard(event, content),
        buildTitle: (event, content) => {
            const weather = buildWeatherCardData(event, content || '');
            if (weather.needsInput) return '天气应用';
            return weather.city ? `${weather.city}天气` : '天气应用';
        },
        buildMessages: buildWeatherBackedA2uiMessages
    },
    {
        id: 'finance.quote.v1',
        kind: 'finance',
        title: '财经应用',
        shortTitle: '财经',
        kicker: 'A2UI / MC Finance App',
        launchTitle: '已找到财经智能体',
        launchVerb: '正在启动财经应用',
        statusLabel: '待专用模板',
        supportLabel: 'MC Markdown 回退',
        autoOpen: false,
        keywords: /财经|股票|股价|报价|证券|基金|指数|雅虎|finance|stock|quote|ticker|nasdaq|price|market/i,
        matcher: (event, content) => matchAgentAppPresetText(event, content, /财经|股票|股价|报价|证券|基金|指数|雅虎|finance|stock|quote|ticker|nasdaq|price|market/i),
        buildMessages: buildPresetMarkdownA2uiMessages
    },
    {
        id: 'game.status.v1',
        kind: 'game',
        title: '游戏应用',
        shortTitle: '游戏',
        kicker: 'A2UI / MC Game App',
        launchTitle: '已找到游戏智能体',
        launchVerb: '正在启动游戏应用',
        statusLabel: '待专用模板',
        supportLabel: 'MC Markdown 回退',
        autoOpen: false,
        keywords: /游戏|steam|补丁|价格|在线人数|玩家|gamer|game|patch|player|online/i,
        matcher: (event, content) => matchAgentAppPresetText(event, content, /游戏|steam|补丁|价格|在线人数|玩家|gamer|game|patch|player|online/i),
        buildMessages: buildPresetMarkdownA2uiMessages
    },
    {
        id: 'search.results.v1',
        kind: 'search',
        title: '搜索应用',
        shortTitle: '搜索',
        kicker: 'A2UI / MC Search App',
        launchTitle: '已找到搜索智能体',
        launchVerb: '正在启动搜索应用',
        statusLabel: '待专用模板',
        supportLabel: 'MC Markdown 回退',
        autoOpen: false,
        keywords: /搜索|检索|论文|arxiv|网页|资料|search|research|paper|browser/i,
        matcher: (event, content) => matchAgentAppPresetText(event, content, /搜索|检索|论文|arxiv|网页|资料|search|research|paper|browser/i),
        buildMessages: buildPresetMarkdownA2uiMessages
    },
    {
        id: 'train.search.v1',
        kind: 'train',
        title: '车次应用',
        shortTitle: '车次',
        kicker: 'A2UI / MC Train App',
        launchTitle: '已找到车次智能体',
        launchVerb: '正在启动车次应用',
        statusLabel: '待专用模板',
        supportLabel: 'MC Markdown 回退',
        autoOpen: false,
        keywords: /火车|高铁|动车|车次|余票|12306|列车|train|rail|ticket/i,
        matcher: (event, content) => matchAgentAppPresetText(event, content, /火车|高铁|动车|车次|余票|12306|列车|train|rail|ticket/i),
        buildMessages: buildPresetMarkdownA2uiMessages
    },
    {
        id: 'json.mc.v1',
        kind: 'json',
        title: 'JSON 渲染',
        shortTitle: 'JSON',
        kicker: 'A2UI / MC JSON Renderer',
        launchTitle: '已识别 JSON 返回',
        launchVerb: '正在启动 JSON 渲染',
        statusLabel: '通用',
        supportLabel: '通用 JSON 渲染',
        autoOpen: false,
        matcher: () => false,
        buildMessages: buildPresetMarkdownA2uiMessages
    },
    {
        id: 'markdown.mc.v1',
        kind: 'generic',
        title: '智能体应用',
        shortTitle: '通用',
        kicker: 'A2UI / MC Agent App',
        launchTitle: '已找到云端智能体',
        launchVerb: '正在启动智能体应用',
        statusLabel: '兜底',
        supportLabel: '通用 Markdown 渲染',
        autoOpen: false,
        matcher: () => false,
        buildMessages: buildPresetMarkdownA2uiMessages
    }
];

// Agent 身份到本地模板的路由表：优先看 AIC，其次 endpoint/name，避免不同 Agent 结果互相覆盖。
const AGENT_APP_IDENTITY_RULES = [
    { presetId: 'finance.quote.v1', pattern: /finance|财经|股票|股价|证券|基金|api\/finance\/aip/i },
    { presetId: 'game.status.v1', pattern: /gamer|游戏|steam|补丁|玩家|api\/gamer\/aip/i },
    { presetId: 'train.search.v1', pattern: /train|车次|火车|高铁|动车|12306|api\/train\/aip/i },
    { presetId: 'search.results.v1', pattern: /searcher|综合搜索|检索|api\/searcher\/aip/i },
    { presetId: 'markdown.mc.v1', pattern: /\/aip\/rpc/i }
];

// 34 个省级行政区行政中心的本地真实地标图册映射；这些条目补足主城市配置没有覆盖的省会/行政中心。
const WEATHER_CHINA_CAPITAL_LANDMARK_ASSETS = [
    { pattern: /天津|tianjin/i, title: '天津', wikiTitles: ['天津市', '天津'], label: '天津城市印象', photoSlug: 'tianjin' },
    { pattern: /石家庄|shijiazhuang/i, title: '石家庄', wikiTitles: ['石家庄市', '石家庄'], label: '石家庄城市印象', photoSlug: 'shijiazhuang' },
    { pattern: /太原|taiyuan/i, title: '太原', wikiTitles: ['太原市', '太原'], label: '太原城市印象', photoSlug: 'taiyuan' },
    { pattern: /呼和浩特|hohhot|huhehaote/i, title: '呼和浩特', wikiTitles: ['呼和浩特市', '呼和浩特'], label: '呼和浩特城市印象', photoSlug: 'hohhot' },
    { pattern: /沈阳|shenyang/i, title: '沈阳', wikiTitles: ['沈阳市', '沈阳'], label: '沈阳城市印象', photoSlug: 'shenyang' },
    { pattern: /长春|changchun/i, title: '长春', wikiTitles: ['长春市', '长春'], label: '长春城市印象', photoSlug: 'changchun' },
    { pattern: /哈尔滨|harbin/i, title: '哈尔滨', wikiTitles: ['哈尔滨市', '哈尔滨'], label: '哈尔滨城市印象', photoSlug: 'harbin' },
    { pattern: /南京|nanjing/i, title: '南京', wikiTitles: ['南京市', '南京'], label: '南京城市印象', photoSlug: 'nanjing' },
    { pattern: /合肥|hefei/i, title: '合肥', wikiTitles: ['合肥市', '合肥'], label: '合肥城市印象', photoSlug: 'hefei' },
    { pattern: /福州|fuzhou/i, title: '福州', wikiTitles: ['福州市', '福州'], label: '福州城市印象', photoSlug: 'fuzhou' },
    { pattern: /南昌|nanchang/i, title: '南昌', wikiTitles: ['南昌市', '南昌'], label: '南昌城市印象', photoSlug: 'nanchang' },
    { pattern: /济南|jinan|ji.?nan/i, title: '济南', wikiTitles: ['济南市', '济南'], label: '济南城市印象', photoSlug: 'jinan' },
    { pattern: /郑州|zhengzhou/i, title: '郑州', wikiTitles: ['郑州市', '郑州'], label: '郑州城市印象', photoSlug: 'zhengzhou' },
    { pattern: /长沙|changsha/i, title: '长沙', wikiTitles: ['长沙市', '长沙'], label: '长沙城市印象', photoSlug: 'changsha' },
    { pattern: /南宁|nanning/i, title: '南宁', wikiTitles: ['南宁市', '南宁'], label: '南宁城市印象', photoSlug: 'nanning' },
    { pattern: /海口|haikou/i, title: '海口', wikiTitles: ['海口市', '海口'], label: '海口城市印象', photoSlug: 'haikou' },
    { pattern: /贵阳|guiyang/i, title: '贵阳', wikiTitles: ['贵阳市', '贵阳'], label: '贵阳城市印象', photoSlug: 'guiyang' },
    { pattern: /昆明|kunming/i, title: '昆明', wikiTitles: ['昆明市', '昆明'], label: '昆明城市印象', photoSlug: 'kunming' },
    { pattern: /拉萨|lhasa/i, title: '拉萨', wikiTitles: ['拉萨市', '拉萨'], label: '拉萨城市印象', photoSlug: 'lhasa' },
    { pattern: /兰州|lanzhou/i, title: '兰州', wikiTitles: ['兰州市', '兰州'], label: '兰州城市印象', photoSlug: 'lanzhou' },
    { pattern: /西宁|xining/i, title: '西宁', wikiTitles: ['西宁市', '西宁'], label: '西宁城市印象', photoSlug: 'xining' },
    { pattern: /银川|yinchuan/i, title: '银川', wikiTitles: ['银川市', '银川'], label: '银川城市印象', photoSlug: 'yinchuan' },
    { pattern: /乌鲁木齐|urumqi|wulumuqi/i, title: '乌鲁木齐', wikiTitles: ['乌鲁木齐市', '乌鲁木齐'], label: '乌鲁木齐城市印象', photoSlug: 'urumqi' },
    { pattern: /台北|taipei/i, title: '台北', wikiTitles: ['台北市', '台北'], label: '台北城市印象', photoSlug: 'taipei' },
    { pattern: /香港|hong\s*kong|hongkong/i, title: '香港', wikiTitles: ['香港', '香港特别行政区'], label: '香港城市印象', photoSlug: 'hongkong' },
    { pattern: /澳门|澳門|macau|macao/i, title: '澳门', wikiTitles: ['澳门', '澳门特别行政区'], label: '澳门城市印象', photoSlug: 'macau' }
];

// 城市素材配置：只影响天气 App 的视觉呈现，优先真实城市照片，真实图不可达时再回退到本地 SVG 图册。
const WEATHER_CITY_VISUAL_CONFIG = {
    provider: 'wikimedia',
    providerLabel: '真实城市照片',
    providerSearchUrl: 'https://zh.wikipedia.org/w/api.php',
    providerTimeoutMs: 2600,
    imageTimeoutMs: 2600,
    districtProviderTimeoutMs: 5600,
    districtImageTimeoutMs: 5600,
    preferRemotePhoto: true,
    preferLocalPhotoAlbum: true,
    useLocalPlaceholder: false,
    fallbackUrls: [
        'assets/weather-cities/city-default.svg'
    ],
    remoteProviders: [
        {
            id: 'wikimedia',
            type: 'wikimedia',
            label: '真实城市照片',
            searchUrl: 'https://zh.wikipedia.org/w/api.php',
            timeoutMs: 2600
        }
    ],
    localAssets: [
        { pattern: /北京|beijing/i, title: '北京', wikiTitles: ['北京市', '北京'], label: '北京城市印象', photoSlug: 'beijing', urls: ['assets/weather-cities/beijing.svg', 'assets/weather-cities/city-default.svg'] },
        { pattern: /上海|shanghai/i, title: '上海', wikiTitles: ['上海市', '上海'], label: '上海城市印象', photoSlug: 'shanghai', urls: ['assets/weather-cities/shanghai.svg', 'assets/weather-cities/city-default.svg'] },
        { pattern: /广州|guangzhou|canton/i, title: '广州', wikiTitles: ['广州市', '广州'], label: '广州城市印象', photoSlug: 'guangzhou', urls: ['assets/weather-cities/guangzhou.svg', 'assets/weather-cities/city-default.svg'] },
        { pattern: /深圳|shenzhen/i, title: '深圳', wikiTitles: ['深圳市', '深圳'], label: '深圳城市印象', urls: ['assets/weather-cities/shenzhen.svg', 'assets/weather-cities/city-default.svg'] },
        { pattern: /杭州|hangzhou/i, title: '杭州', wikiTitles: ['杭州市', '杭州'], label: '杭州城市印象', photoSlug: 'hangzhou', urls: ['assets/weather-cities/hangzhou.svg', 'assets/weather-cities/city-default.svg'] },
        { pattern: /成都|chengdu/i, title: '成都', wikiTitles: ['成都市', '成都'], label: '成都城市印象', photoSlug: 'chengdu', urls: ['assets/weather-cities/chengdu.svg', 'assets/weather-cities/city-default.svg'] },
        { pattern: /重庆|chongqing/i, title: '重庆', wikiTitles: ['重庆市', '重庆'], label: '重庆城市印象', photoSlug: 'chongqing', urls: ['assets/weather-cities/chongqing.svg', 'assets/weather-cities/city-default.svg'] },
        { pattern: /武汉|wuhan/i, title: '武汉', wikiTitles: ['武汉市', '武汉'], label: '武汉城市印象', photoSlug: 'wuhan', urls: ['assets/weather-cities/wuhan.svg', 'assets/weather-cities/city-default.svg'] },
        { pattern: /西安|xi.?an|xian/i, title: '西安', wikiTitles: ['西安市', '西安'], label: '西安城市印象', photoSlug: 'xian', urls: ['assets/weather-cities/xian.svg', 'assets/weather-cities/city-default.svg'] },
        ...WEATHER_CHINA_CAPITAL_LANDMARK_ASSETS,
        { pattern: /潮州|chaozhou/i, title: '潮州', wikiTitles: ['潮州市', '潮州'], label: '潮州城市印象', urls: ['assets/weather-cities/chaozhou.svg', 'assets/weather-cities/city-default.svg'] }
    ]
};

// 城市素材缓存：避免同一城市在多次 A2UI 更新中重复请求远程素材。
const weatherCityVisualCache = new Map();

// 工具函数
function getAuthHeaders() {
    const token = localStorage.getItem('access_token');
    return token ? { 'Authorization': `Bearer ${token}` } : {};
}

function applyQuotaHeaders(response) {
    if (!state.user || state.user.role === 'admin') return;
    const limit = Number(response.headers.get('X-Quota-Limit'));
    const used = Number(response.headers.get('X-Quota-Used'));
    if (!Number.isFinite(limit) || !Number.isFinite(used)) return;
    state.user.daily_limit = limit;
    state.user.today_used = used;
    updateUserUI();
}

let tokenRefreshTimer = null;
let tokenRefreshInFlight = null;

function getJwtExpiryMs(token) {
    try {
        const payload = token.split('.')[1];
        const base64 = payload.replace(/-/g, '+').replace(/_/g, '/');
        const normalized = base64.padEnd(base64.length + ((4 - base64.length % 4) % 4), '=');
        const decoded = JSON.parse(atob(normalized));
        return Number(decoded.exp || 0) * 1000;
    } catch (error) {
        return 0;
    }
}

function clearAuthSession() {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    if (tokenRefreshTimer) {
        clearTimeout(tokenRefreshTimer);
        tokenRefreshTimer = null;
    }
}

function scheduleTokenRefresh() {
    if (tokenRefreshTimer) clearTimeout(tokenRefreshTimer);
    const expiryMs = getJwtExpiryMs(localStorage.getItem('access_token') || '');
    if (!expiryMs || !localStorage.getItem('refresh_token')) return;
    // Refresh one minute before expiry. Browser timer throttling is covered by
    // the visibility/focus and request-time checks below.
    const delay = Math.max(0, expiryMs - Date.now() - 60_000);
    tokenRefreshTimer = setTimeout(() => {
        refreshAccessToken().catch(() => {});
    }, Math.min(delay, 2_147_000_000));
}

async function refreshAccessToken() {
    if (tokenRefreshInFlight) return tokenRefreshInFlight;
    const refreshToken = localStorage.getItem('refresh_token');
    if (!refreshToken) return false;

    tokenRefreshInFlight = (async () => {
        try {
            const response = await fetch(`${API_BASE}/auth/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh_token: refreshToken })
            });
            if (!response.ok) {
                clearAuthSession();
                state.user = null;
                updateUserUI();
                return false;
            }
            const data = await response.json();
            localStorage.setItem('access_token', data.access_token);
            if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token);
            if (data.user) state.user = data.user;
            scheduleTokenRefresh();
            return true;
        } catch (error) {
            // A temporary network outage must not discard an otherwise valid
            // refresh token. The next foreground/request-time check retries.
            console.warn('[Dingdang] 刷新登录状态失败:', error);
            return false;
        } finally {
            tokenRefreshInFlight = null;
        }
    })();
    return tokenRefreshInFlight;
}

async function ensureFreshAccessToken() {
    const token = localStorage.getItem('access_token');
    if (!token) return false;
    const expiryMs = getJwtExpiryMs(token);
    if (expiryMs && expiryMs - Date.now() > 60_000) return true;
    const refreshed = await refreshAccessToken();
    if (!refreshed && !localStorage.getItem('refresh_token')) {
        clearAuthSession();
        state.user = null;
        updateUserUI();
    }
    return refreshed;
}

function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

function formatDateTime(dateStr) {
    try {
        const date = new Date(dateStr);
        if (isNaN(date.getTime())) {
            return '无效日期';
        }
        // 转换为本地时区并显示详细格式：YYYY-MM-DD HH:mm:ss
        return date.toLocaleString('zh-CN', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false
        }).replace(/\//g, '-');
    } catch {
        return '无效日期';
    }
}

// DOM 元素
const elements = {
    chatShell: document.querySelector('.chat-shell'),
    conversationSidebar: document.getElementById('conversationSidebar'),
    sidebarToggle: document.getElementById('sidebarToggle'),
    sidebarBackdrop: document.getElementById('sidebarBackdrop'),
    sidebarAccountArea: document.getElementById('sidebarAccountArea'),
    sidebarAccountButton: document.getElementById('sidebarAccountButton'),
    sidebarAccountMenu: document.getElementById('sidebarAccountMenu'),
    sidebarAccountAvatar: document.getElementById('sidebarAccountAvatar'),
    sidebarAccountName: document.getElementById('sidebarAccountName'),
    sidebarAccountMeta: document.getElementById('sidebarAccountMeta'),
    sidebarAccountProfile: document.getElementById('sidebarAccountProfile'),
    sidebarAccountAdmin: document.getElementById('sidebarAccountAdmin'),
    sidebarAccountAdminUsers: document.getElementById('sidebarAccountAdminUsers'),
    sidebarAccountAdminLogs: document.getElementById('sidebarAccountAdminLogs'),
    sidebarAccountLogin: document.getElementById('sidebarAccountLogin'),
    sidebarAccountLogout: document.getElementById('sidebarAccountLogout'),
    sidebarDeveloperTools: document.getElementById('sidebarDeveloperTools'),
    developerToolsModal: document.getElementById('developerToolsModal'),
    closeDeveloperToolsModal: document.getElementById('closeDeveloperToolsModal'),
    developerAcsInput: document.getElementById('developerAcsInput'), developerMessageInput: document.getElementById('developerMessageInput'), developerSendBtn: document.getElementById('developerSendBtn'), developerRawResponse: document.getElementById('developerRawResponse'), developerClearBtn: document.getElementById('developerClearBtn'), developerDebugStatus: document.getElementById('developerDebugStatus'), developerLogsPanel: document.getElementById('developerLogsPanel'), developerDebugPanel: document.getElementById('developerDebugPanel'), developerLogsOutput: document.getElementById('developerLogsOutput'),
    themeMenuButton: document.getElementById('sidebarAccountThemeButton'),
    themeMenu: document.getElementById('themeMenu'),
    themeDark: document.getElementById('themeDark'),
    themeLight: document.getElementById('themeLight'),
    themeSystem: document.getElementById('themeSystem'),
    loginBtn: document.getElementById('loginBtn'),
    headerRight: document.getElementById('headerRight'),
    dingdangAvatar: document.querySelector('.dingdang-avatar'),
    authModal: document.getElementById('authModal'),
    closeModal: document.getElementById('closeModal'),
    authForm: document.getElementById('authForm'),
    authSubmit: document.getElementById('authSubmit'),
    modalTitle: document.getElementById('modalTitle'),
    switchMode: document.getElementById('switchMode'),
    forgotPasswordLink: document.getElementById('forgotPasswordLink'),
    switchText: document.getElementById('switchText'),
    usernameInput: document.getElementById('username'),
    passwordInput: document.getElementById('password'),
    passwordHint: document.getElementById('passwordHint'),
    agentRenderModeControl: document.getElementById('agentRenderModeControl'),
    agentRenderToggle: document.getElementById('agentRenderToggle'),
    agentRenderToggleLabel: document.getElementById('agentRenderToggleLabel'),
    agentRenderModeHint: document.getElementById('agentRenderModeHint'),
    agentInteractionStatus: document.getElementById('agentInteractionStatus'),
    agentInteractionStatusTitle: document.getElementById('agentInteractionStatusTitle'),
    agentInteractionStatusHint: document.getElementById('agentInteractionStatusHint'),
    agentInteractionEndBtn: document.getElementById('agentInteractionEndBtn'),
    messageInput: document.getElementById('messageInput'),
    sendBtn: document.getElementById('sendBtn'),
    imageUploadBtn: document.getElementById('imageUploadBtn'),
    imageInput: document.getElementById('imageInput'),
    imagePreviewWrapper: document.getElementById('imagePreviewWrapper'),
    imagePreview: document.getElementById('imagePreview'),
    imageRemoveBtn: document.getElementById('imageRemoveBtn'),
    imageUploadingOverlay: document.getElementById('imageUploadingOverlay'),
    attachmentPreview: document.getElementById('attachmentPreview'),
    attachmentPreviewName: document.getElementById('attachmentPreviewName'),
    attachmentPreviewStatus: document.getElementById('attachmentPreviewStatus'),
    attachmentRemoveBtn: document.getElementById('attachmentRemoveBtn'),
    messagesContainer: document.getElementById('messagesContainer'),
    messageImageLightbox: document.getElementById('messageImageLightbox'),
    messageImageLightboxClose: document.getElementById('messageImageLightboxClose'),
    messageImageLightboxImage: document.getElementById('messageImageLightboxImage'),
    messageImageLightboxTitle: document.getElementById('messageImageLightboxTitle'),
    messageResourceStatus: document.getElementById('messageResourceStatus'),
    recentAgentAppBtn: document.getElementById('recentAgentAppBtn'),
    recentAgentAppDismissBtn: document.getElementById('recentAgentAppDismissBtn'),
    recentAgentAppTitle: document.getElementById('recentAgentAppTitle'),
    recentAgentAppMeta: document.getElementById('recentAgentAppMeta'),
    userInfoModal: document.getElementById('userInfoModal'),
    closeUserInfoModal: document.getElementById('closeUserInfoModal'),
    userInfoAvatar: document.getElementById('userInfoAvatar'),
    userInfoName: document.getElementById('userInfoName'),
    userInfoStatus: document.getElementById('userInfoStatus'),
    userInfoAic: document.getElementById('userInfoAic'),
    copyUserInfoAic: document.getElementById('copyUserInfoAic'),
    userInfoAicFeedback: document.getElementById('userInfoAicFeedback'),
    changePasswordEntryBtn: document.getElementById('changePasswordEntryBtn'),
    userInfoAdminActions: document.getElementById('userInfoAdminActions'),
    profileAdminUsersBtn: document.getElementById('profileAdminUsersBtn'),
    profileAdminLogsBtn: document.getElementById('profileAdminLogsBtn'),
    userInfoView: document.getElementById('userInfoView'),
    changePasswordView: document.getElementById('changePasswordView'),
    backToUserInfoBtn: document.getElementById('backToUserInfoBtn'),
    changePasswordForm: document.getElementById('changePasswordForm'),
    currentPassword: document.getElementById('currentPassword'),
    currentPasswordGroup: document.getElementById('currentPasswordGroup'),
    newPassword: document.getElementById('newPassword'),
    newPasswordGroup: document.getElementById('newPasswordGroup'),
    confirmPassword: document.getElementById('confirmPassword'),
    confirmPasswordGroup: document.getElementById('confirmPasswordGroup'),
    passwordStrengthBar: document.getElementById('passwordStrengthBar'),
    passwordStrengthLabel: document.getElementById('passwordStrengthLabel'),
    passwordLengthHint: document.getElementById('passwordLengthHint'),
    passwordMismatch: document.getElementById('passwordMismatch'),
    changePasswordSubmit: document.getElementById('changePasswordSubmit'),
    changePasswordMessage: document.getElementById('changePasswordMessage'),
    adminUsersModal: document.getElementById('adminUsersModal'),
    closeAdminUsersModal: document.getElementById('closeAdminUsersModal'),
    adminUserSearchInput: document.getElementById('adminUserSearchInput'),
    adminUserSearchBtn: document.getElementById('adminUserSearchBtn'),
    adminUsersSummary: document.getElementById('adminUsersSummary'),
    adminUsersTableBody: document.getElementById('adminUsersTableBody'),
    adminPrevPageBtn: document.getElementById('adminPrevPageBtn'),
    adminNextPageBtn: document.getElementById('adminNextPageBtn'),
    adminPageInfo: document.getElementById('adminPageInfo'),
    adminLogsModal: document.getElementById('adminLogsModal'),
    closeAdminLogsModal: document.getElementById('closeAdminLogsModal'),
    adminLogsUsername: document.getElementById('adminLogsUsername'),
    adminLogsUserId: document.getElementById('adminLogsUserId'),
    adminLogsEventType: document.getElementById('adminLogsEventType'),
    adminLogsStartTime: document.getElementById('adminLogsStartTime'),
    adminLogsEndTime: document.getElementById('adminLogsEndTime'),
    adminLogsKeyword: document.getElementById('adminLogsKeyword'),
    adminLogsSearchBtn: document.getElementById('adminLogsSearchBtn'),
    adminLogsSummary: document.getElementById('adminLogsSummary'),
    adminLogsTableBody: document.getElementById('adminLogsTableBody'),
    adminLogsPrevPageBtn: document.getElementById('adminLogsPrevPageBtn'),
    adminLogsNextPageBtn: document.getElementById('adminLogsNextPageBtn'),
    adminLogsPageInfo: document.getElementById('adminLogsPageInfo'),

    // 随意探索按钮
    exploreBtn: document.getElementById('exploreBtn'),
    // 清空对话区按钮
    clearChatBtn: document.getElementById('clearChatBtn'),
    recentConversations: document.getElementById('recentConversations'),
    // Tab 视图
    chatView: document.getElementById('chatView'),
    discoverView: document.getElementById('discoverView'),
    profileView: document.getElementById('profileView'),
    // 发现页
    discoverSearchInput: document.getElementById('discoverSearchInput'),
    discoverSearchBtn: document.getElementById('discoverSearchBtn'),
    discoverMoreBtn: document.getElementById('discoverMoreBtn'),
    discoverAgentsGrid: document.getElementById('discoverAgentsGrid'),
    categoryTags: document.querySelectorAll('.category-tag'),
    // 我的页
    profileAvatar: document.getElementById('profileAvatar'),
    profileUsername: document.getElementById('profileUsername'),
    profileRole: document.getElementById('profileRole'),
    profileLogoutBtn: document.getElementById('profileLogoutBtn'),
    menuSettings: document.getElementById('menuSettings'),
    menuAbout: document.getElementById('menuAbout'),
    capabilityLaunchOverlay: document.getElementById('capabilityLaunchOverlay'),
    capabilityLaunchTitle: document.getElementById('capabilityLaunchTitle'),
    capabilityLaunchSubtitle: document.getElementById('capabilityLaunchSubtitle'),
    capabilityLaunchAgent: document.getElementById('capabilityLaunchAgent'),
    capabilityLaunchTarget: document.getElementById('capabilityLaunchTarget'),
    weatherAgentAppOverlay: document.getElementById('weatherAgentAppOverlay'),
    weatherAgentBackBtn: document.getElementById('weatherAgentBackBtn'),
    weatherAgentAppKicker: document.getElementById('weatherAgentAppKicker'),
    weatherAgentAppTitle: document.getElementById('weatherAgentAppTitle'),
    weatherAgentAppState: document.getElementById('weatherAgentAppState'),
    weatherAgentAppAgent: document.getElementById('weatherAgentAppAgent'),
    weatherAgentAppTask: document.getElementById('weatherAgentAppTask'),
    agentAppInvocationSummary: document.getElementById('agentAppInvocationSummary'),
    agentAppInteractionPanel: document.getElementById('agentAppInteractionPanel'),
    agentAppPresetSummary: document.getElementById('agentAppPresetSummary'),
    agentAppPresetList: document.getElementById('agentAppPresetList'),
    agentAppPageSummary: document.getElementById('agentAppPageSummary'),
    agentAppPageList: document.getElementById('agentAppPageList'),
    weatherAgentAppBody: document.querySelector('.weather-agent-app-body'),
    weatherAgentAppSurface: document.getElementById('weatherAgentAppSurface'),
    weatherAgentAppEvidence: document.getElementById('weatherAgentAppEvidence')
};

// 模态框状态
let isLoginMode = true;

// 嵌入模式允许的来源：只接受本地调试页和 WT，避免任意站点驱动叮当发起调用。
const EMBED_ALLOWED_ORIGINS = [
    'http://localhost:3000',
    'http://localhost:8080',
    'http://192.0.2.30:8000',
    'https://wt.ioa.pub'
];

// 规范化外部页面传入的 Agent 描述，保证后续调用只依赖稳定字段。
function normalizeEmbeddedAgent(agent) {
    if (!agent || typeof agent !== 'object') return null;

    const normalized = {
        name: agent.name || '',
        description: agent.description || '',
        type: agent.type || '',
        url: agent.url || '',
        aic: agent.aic || '',
        endpoint: agent.endpoint || '',
        assigned_task: agent.assigned_task || ''
    };

    if (!normalized.name || !normalized.type) {
        return null;
    }
    return normalized;
}

// 草稿态绝不生成伪会话 ID。首次实际调用由后端创建并通过 SSE 返回真实 ID。
function ensureConversationId() {
    return state.currentConversationId;
}

// 外部首页每次发起调用都从独立草稿开始，确认卡不应要求用户已登录。
// 首次实际发送或执行仍由既有流程创建并绑定真实会话。
function createEmbeddedConversation() {
    startNewConversation();
    return ensureConversationId();
}

// 处理外部页面嵌入请求：先展示可编辑的确认卡，绝不因首页点击而自动执行 AIP 调用。
async function invokeEmbeddedAgent(agent, task = '') {
    const normalizedAgent = normalizeEmbeddedAgent(agent);
    if (!normalizedAgent) {
        throw new Error('Invalid agent payload');
    }

    const conversationId = createEmbeddedConversation();
    switchTab('chat');

    if (elements.messagesContainer.querySelector('.welcome-message')) {
        clearDynamicActionElement(elements.messagesContainer);
    }

    const introHtml = `
        <div class="message assistant">
            <div class="message-avatar">
                <img src="dingdang.png" alt="叮当">
            </div>
            <div class="message-content-wrapper">
                <div class="message-content">已接收外部页面发起的协作请求，将由叮当协调 1 个智能体：${escapeHTML(normalizedAgent.name)}</div>
            </div>
        </div>
    `;
    elements.messagesContainer.insertAdjacentHTML('beforeend', introHtml);

    if (!['web', 'ioa'].includes(normalizedAgent.type)) {
        throw new Error(`Unsupported agent type: ${normalizedAgent.type}`);
    }

    const executionTask = task || normalizedAgent.assigned_task || `调用智能体「${normalizedAgent.name}」`;
    showEmbeddedAgentConfirmation(normalizedAgent, executionTask);
    scrollToBottom();

    return {
        ok: true,
        mode: 'confirmation',
        conversationId,
        pending_confirmation: true
    };
}

function showEmbeddedAgentConfirmation(agent, task) {
    const wrapper = document.createElement('section');
    wrapper.className = 'embedded-agent-confirmation';
    wrapper.setAttribute('aria-label', `确认由叮当协调 ${agent.name}`);
    wrapper.innerHTML = createSingleAgentCardHTML(agent, task);
    elements.messagesContainer.appendChild(wrapper);

    const card = wrapper.querySelector('.single-agent-card');
    const cancelButton = wrapper.querySelector('.btn-cancel-agent-invoke');
    const invokeButton = wrapper.querySelector('.btn-invoke-agent');
    const openButton = wrapper.querySelector('.btn-open-web-agent');

    cancelButton?.addEventListener('click', () => {
        wrapper.remove();
        if (!elements.messagesContainer.children.length) renderWelcome();
    });

    if (agent.type === 'ioa' && invokeButton) {
        invokeButton.addEventListener('click', () => handleSingleAgentInvoke(card, agent));
    }
    if (agent.type === 'web' && openButton) {
        openButton.addEventListener('click', () => {
            const target = String(agent.url || '').trim();
            if (!target) return;
            window.open(target, '_blank', 'noopener,noreferrer');
        });
    }
}

// 父页先以 PING 确认 iframe 已装载消息桥；只有收到 READY 后才会发送调用请求。
function registerEmbedReadinessListener() {
    window.addEventListener('message', (event) => {
        if (!EMBED_ALLOWED_ORIGINS.includes(event.origin)) return;
        if (event.data?.type !== 'DD_EMBED_PING') return;
        event.source?.postMessage({
            type: 'DD_EMBED_READY',
            payload: { ready: true }
        }, event.origin);
    });
}

// 注册 postMessage 调用桥，供父页面在 iframe 中启动叮当智能体调用。
function registerEmbedMessageListener() {
    window.addEventListener('message', async (event) => {
        if (!EMBED_ALLOWED_ORIGINS.includes(event.origin)) {
            return;
        }

        const data = event.data;
        if (!data || typeof data !== 'object' || data.type !== 'DD_EXECUTE_AGENT') {
            return;
        }

        try {
            const payload = data.payload || {};
            const result = await invokeEmbeddedAgent(payload.agent, payload.task || '');
            event.source?.postMessage({
                type: 'DD_EXECUTE_AGENT_ACK',
                payload: {
                    ok: true,
                    result
                }
            }, event.origin);
        } catch (error) {
            console.error('Embedded invoke failed:', error);
            event.source?.postMessage({
                type: 'DD_EXECUTE_AGENT_ACK',
                payload: {
                    ok: false,
                    error: error.message || 'invoke failed'
                }
            }, event.origin);
        }
    });
}

// 初始化
async function init() {
    initializeTheme();
    await loadAuthProvider();
    checkAuth();
    setupEventListeners();
    setupDynamicActionDelegation();
    setupThemeControls();
    setupSidebarAccountControls();
    setupMobileSidebarControls();
    document.getElementById('healthRefreshBtn')?.addEventListener('click', refreshHealthMonitor);
    registerEmbedReadinessListener();
    registerEmbedMessageListener();
}

async function loadAuthProvider() {
    try {
        const response = await fetch(`${API_BASE}/auth/config`);
        if (response.ok) {
            const config = await response.json();
            state.authProvider = config.provider === 'keycloak' ? 'keycloak' : 'sqlite';
        }
    } catch (error) {
        console.warn('[Dingdang] 无法读取认证配置，沿用本地登录界面:', error);
    }
    setAuthMode();
}

function getDynamicActionPayload(target) {
    const key = target?.dataset?.actionPayload;
    const payload = key ? dynamicActionPayloads.get(key) : null;
    if (!payload || payload.action !== target?.dataset?.uiAction) return null;
    return payload;
}

function setupDynamicActionDelegation() {
    if (dynamicActionDelegationInitialized) return;
    dynamicActionDelegationInitialized = true;
    document.addEventListener('click', event => {
        const target = event.target.closest?.('[data-ui-action]');
        const payload = getDynamicActionPayload(target);
        if (!target || !payload) return;
        if (payload.action === 'copy-message') {
            copyMessage(target, payload.text);
        } else if (payload.action === 'download-agent-artifact') {
            downloadAgentArtifact(payload.artifactId, payload.filename);
        } else if (payload.action === 'submit-weather-input') {
            submitWeatherAgentInput(payload.cardRef);
        } else if (payload.action === 'use-weather-location') {
            useCurrentLocationForWeatherAgent(payload.cardRef);
        } else if (payload.action === 'submit-agent-continuation') {
            submitAgentContinuation(payload.cardDomId);
        } else if (payload.action === 'open-discovery-web') {
            openDiscoveryWebAgent(payload.panelId, payload.index);
        } else if (payload.action === 'show-agent-catalog') {
            showAgentCatalogModal(payload.kind, payload.panelId);
        } else if (payload.action === 'execute-selected-agents') {
            executeSelectedAgents(target, { panelId: payload.panelId });
        } else if (payload.action === 'confirm-recommended-agents') {
            confirmRecommendedAgents(payload.panelId);
        } else if (payload.action === 'show-catalog-details') {
            if (event.target.closest?.('[data-agent-catalog-checkbox]')) return;
            showAgentCatalogDetails(payload.kind, payload.index);
        } else if (payload.action === 'set-agent-catalog-page') {
            setAgentCatalogPage(payload.kind, payload.page);
        } else if (payload.action === 'confirm-ioa-catalog') {
            confirmIoaCatalogSelection();
        } else if (payload.action === 'confirm-web-catalog') {
            confirmWebCatalogSelection();
        } else if (payload.action === 'close-agent-catalog') {
            closeAgentCatalogModal();
        } else if (payload.action === 'toggle-agent-catalog-all') {
            toggleAgentCatalogSelectAll(payload.kind);
        } else if (payload.action === 'toggle-json-summary') {
            toggleChatJsonSummary(payload.rawId, target);
        } else if (payload.action === 'confirm-secretary-completion') {
            confirmSecretaryCompletion();
        } else if (payload.action === 'continue-secretary-completion') {
            continueSecretaryCompletion();
        } else if (payload.action === 'confirm-agent-completion') {
            confirmExternalAgentCompletion();
        } else if (payload.action === 'continue-agent-completion') {
            continueExternalAgentCompletion();
        }
    });
    document.addEventListener('change', event => {
        const checkbox = event.target.closest?.('[data-agent-catalog-checkbox]');
        if (checkbox) toggleAgentCatalogSelection(checkbox.value, checkbox.checked);
    });
    document.addEventListener('input', event => {
        const editor = event.target.closest?.('[data-agent-catalog-task-index]');
        if (editor) updateAgentCatalogTask(Number(editor.dataset.agentCatalogTaskIndex), editor.value);
    });
    document.addEventListener('keydown', event => {
        const target = event.target.closest?.('[data-ui-action]');
        const payload = getDynamicActionPayload(target);
        if (!target || !payload) return;
        if (payload.action === 'weather-input-keydown') {
            handleWeatherInputKeydown(event, payload.cardRef);
        } else if (payload.action === 'agent-continuation-keydown') {
            handleAgentContinuationKeydown(event, payload.cardDomId);
        }
    });
}

function closeThemeMenu({ restoreFocus = false } = {}) {
    if (!elements.themeMenu || !elements.themeMenuButton) return;
    elements.themeMenu.hidden = true;
    elements.themeMenuButton.setAttribute('aria-expanded', 'false');
    if (restoreFocus) elements.themeMenuButton.focus();
}

function openThemeMenu() {
    if (!elements.themeMenu || !elements.themeMenuButton) return;
    elements.themeMenu.hidden = false;
    elements.themeMenuButton.setAttribute('aria-expanded', 'true');
    const selected = Array.from(elements.themeMenu.querySelectorAll('[data-theme-value]'))
        .find(button => button.getAttribute('aria-checked') === 'true');
    (selected || elements.themeMenu.querySelector('[data-theme-value]'))?.focus();
}

function setupThemeControls() {
    if (themeControlsInitialized || !elements.themeMenuButton || !elements.themeMenu) return;
    themeControlsInitialized = true;
    elements.themeMenuButton.addEventListener('click', event => {
        event.stopPropagation();
        if (elements.themeMenu.hidden) openThemeMenu();
        else closeThemeMenu({ restoreFocus: true });
    });
    const items = Array.from(elements.themeMenu.querySelectorAll('[data-theme-value]'));
    items.forEach((button, index) => {
        button.addEventListener('click', () => {
            setThemePreference(button.dataset.themeValue);
            closeThemeMenu({ restoreFocus: true });
        });
        button.addEventListener('keydown', event => {
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            let nextIndex = index;
            if (event.key === 'ArrowDown') nextIndex = (index + 1) % items.length;
            if (event.key === 'ArrowUp') nextIndex = (index - 1 + items.length) % items.length;
            if (event.key === 'Home') nextIndex = 0;
            if (event.key === 'End') nextIndex = items.length - 1;
            items[nextIndex]?.focus();
        });
    });
    document.addEventListener('click', event => {
        if (!elements.themeMenu.hidden && !event.target.closest?.('.theme-picker')) closeThemeMenu();
    });
}

function closeSidebarAccountMenu({ restoreFocus = false } = {}) {
    if (!elements.sidebarAccountMenu || !elements.sidebarAccountButton) return;
    closeThemeMenu();
    elements.sidebarAccountMenu.hidden = true;
    elements.sidebarAccountButton.setAttribute('aria-expanded', 'false');
    if (restoreFocus) elements.sidebarAccountButton.focus();
}

function openSidebarAccountMenu() {
    if (!elements.sidebarAccountMenu || !elements.sidebarAccountButton) return;
    elements.sidebarAccountMenu.hidden = false;
    elements.sidebarAccountButton.setAttribute('aria-expanded', 'true');
    (elements.sidebarAccountProfile?.hidden ? elements.sidebarAccountLogin : elements.sidebarAccountProfile)?.focus();
}

function setupSidebarAccountControls() {
    if (sidebarAccountControlsInitialized || !elements.sidebarAccountButton || !elements.sidebarAccountMenu) return;
    sidebarAccountControlsInitialized = true;
    elements.sidebarAccountButton.addEventListener('click', event => {
        event.stopPropagation();
        if (elements.sidebarAccountMenu.hidden) openSidebarAccountMenu();
        else closeSidebarAccountMenu({ restoreFocus: true });
    });
    elements.sidebarAccountProfile?.addEventListener('click', () => {
        closeSidebarAccountMenu();
        showUserInfoModal();
    });
    elements.sidebarAccountLogin?.addEventListener('click', () => {
        closeSidebarAccountMenu();
        showAuthModal();
    });
    elements.sidebarAccountLogout?.addEventListener('click', () => {
        closeSidebarAccountMenu();
        logout();
    });
    elements.sidebarAccountAdminUsers?.addEventListener('click', () => {
        closeSidebarAccountMenu();
        showAdminUsersModal();
    });
    elements.sidebarAccountAdminLogs?.addEventListener('click', () => {
        closeSidebarAccountMenu();
        showAdminLogsModal();
    });
    document.addEventListener('click', event => {
        if (!elements.sidebarAccountMenu.hidden && !event.target.closest?.('#sidebarAccountArea')) closeSidebarAccountMenu();
    });
}

function setMobileSidebarOpen(open, { manageFocus = true } = {}) {
    if (!mobileSidebarQuery && typeof window.matchMedia === 'function') {
        mobileSidebarQuery = window.matchMedia('(max-width: 48rem)');
    }
    const isMobile = Boolean(mobileSidebarQuery?.matches);
    const isOpen = isMobile && Boolean(open);
    const sidebarHidden = isMobile && !isOpen;
    elements.chatShell?.classList.toggle('sidebar-open', isOpen);
    elements.sidebarToggle?.setAttribute('aria-expanded', String(isOpen));
    elements.sidebarToggle?.setAttribute('aria-label', isOpen ? '关闭对话侧边栏' : '打开对话侧边栏');
    if (elements.conversationSidebar) {
        elements.conversationSidebar.inert = sidebarHidden;
        if (sidebarHidden) elements.conversationSidebar.setAttribute('aria-hidden', 'true');
        else elements.conversationSidebar.removeAttribute('aria-hidden');
    }
    if (elements.sidebarBackdrop) {
        elements.sidebarBackdrop.inert = !isOpen;
        elements.sidebarBackdrop.tabIndex = isOpen ? 0 : -1;
        elements.sidebarBackdrop.setAttribute('aria-hidden', String(!isOpen));
    }
    if (manageFocus && isMobile) {
        if (isOpen) (elements.clearChatBtn || elements.exploreBtn)?.focus();
        else elements.sidebarToggle?.focus();
    }
    return isOpen;
}

function setupMobileSidebarControls() {
    if (shellControlsInitialized) return;
    shellControlsInitialized = true;
    setMobileSidebarOpen(false, { manageFocus: false });
    mobileSidebarQuery?.addEventListener?.('change', () => {
        setMobileSidebarOpen(false, { manageFocus: false });
    });
    elements.sidebarToggle?.addEventListener('click', () => {
        setMobileSidebarOpen(!elements.chatShell?.classList.contains('sidebar-open'));
    });
    elements.sidebarBackdrop?.addEventListener('click', () => setMobileSidebarOpen(false));
    elements.clearChatBtn?.addEventListener('click', () => setMobileSidebarOpen(false));
    elements.exploreBtn?.addEventListener('click', () => setMobileSidebarOpen(false));
}

// 根据当前偏好刷新开关文案与可访问状态，保持移动端 WebView 和浏览器表现一致。
function syncRealtimeAgentRenderToggle() {
    const enabled = Boolean(state.realtimeAgentCardRendering);
    if (elements.agentRenderModeControl) {
        elements.agentRenderModeControl.classList.toggle('is-off', !enabled);
    }
    if (elements.agentRenderToggle) {
        elements.agentRenderToggle.classList.toggle('is-on', enabled);
        elements.agentRenderToggle.setAttribute('aria-pressed', String(enabled));
        elements.agentRenderToggle.setAttribute('aria-label', enabled ? '关闭实时卡片渲染' : '开启实时卡片渲染');
    }
    if (elements.agentRenderToggleLabel) {
        elements.agentRenderToggleLabel.textContent = enabled ? '开启' : '关闭';
    }
    if (elements.agentRenderModeHint) {
        elements.agentRenderModeHint.textContent = enabled
            ? '开启后将 ACPs 返回结果渲染为 A2UI 应用，可恢复已缓存页面'
            : '关闭后当前应用还原为 ACPs/AIP Markdown 原始返回';
    }
}

// 切换实时卡片渲染模式；关闭和开启都只切换同一份 AIP 缓存的展示视图，不重新调用 Agent。
function setRealtimeAgentRenderingEnabled(enabled, options = {}) {
    state.realtimeAgentCardRendering = false;
    closeAgentApp();
    elements.recentAgentAppBtn?.classList.add('is-hidden');
    return false;
}

// 响应用户点击渲染开关，避免发送请求时误触输入框。
function toggleRealtimeAgentRendering(event) {
    event?.preventDefault?.();
    setRealtimeAgentRenderingEnabled(!state.realtimeAgentCardRendering);
}

// 检查登录状态
async function checkAuth() {
    const token = localStorage.getItem('access_token');
    if (token) {
        try {
            if (!await ensureFreshAccessToken()) return;
            const response = await fetch(`${API_BASE}/auth/me`, {
                headers: getAuthHeaders()
            });
            if (response.ok) {
                state.user = await response.json();
                updateUserUI();
                loadConversationHistory();
                startHealthMonitoring();
                scheduleTokenRefresh();
            } else {
                console.log('Token validation failed:', response.status);
                if (await refreshAccessToken()) {
                    updateUserUI();
                } else {
                    clearAuthSession();
                }
            }
        } catch (e) {
            console.error('Auth check error:', e);
        }
    }
}

function renderHealthMonitor(payload) {
    const monitor = document.getElementById('healthMonitor');
    const dot = document.getElementById('healthMonitorDot');
    const text = document.getElementById('healthMonitorText');
    const details = document.getElementById('healthMonitorDetails');
    if (!monitor || !dot || !text || !details) return;
    monitor.hidden = false;
    const overall = payload?.status || 'unhealthy';
    dot.className = `health-monitor-dot is-${overall}`;
    text.textContent = overall === 'healthy' ? '服务正常' : (overall === 'warning' ? '需要关注' : '服务异常');
    details.innerHTML = (payload?.components || []).map(item => {
        const detail = item.detail || (item.days_remaining === undefined ? item.status : `剩余 ${item.days_remaining} 天`);
        return `<div><strong>${escapeHTML(item.label)}</strong><span>${escapeHTML(detail)}</span></div>`;
    }).join('') || '<div>暂无状态数据</div>';
}

async function refreshHealthMonitor() {
    if (!state.user || state.health.refreshing) return;
    state.health.refreshing = true;
    try {
        const response = await fetch(`${API_BASE}/health/components`, { headers: getAuthHeaders() });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        renderHealthMonitor(await response.json());
    } catch (error) {
        renderHealthMonitor({ status: 'unhealthy', components: [{ label: '健康监测', detail: '暂时不可用', status: 'unhealthy' }] });
    } finally { state.health.refreshing = false; }
}

function startHealthMonitoring() {
    if (state.health.timer) return;
    refreshHealthMonitor();
    state.health.timer = window.setInterval(refreshHealthMonitor, 600000);
}

// 更新用户 UI
function updateUserUI() {
    if (state.user) {
        startHealthMonitoring();
        elements.headerRight.replaceChildren();
        const username = state.user.username || '用户';
        const isAdmin = state.user.role === 'admin';
        const dailyLimit = state.user.daily_limit || 10;
        const todayUsed = state.user.today_used || 0;
        const remaining = Math.max(0, dailyLimit - todayUsed);
        if (elements.sidebarAccountAvatar) elements.sidebarAccountAvatar.textContent = username.charAt(0).toUpperCase();
        if (elements.sidebarAccountName) elements.sidebarAccountName.textContent = username;
        if (elements.sidebarAccountMeta) elements.sidebarAccountMeta.textContent = isAdmin ? '管理员 · 无使用限制' : `今日剩余 ${remaining} 次`;
        if (elements.sidebarAccountProfile) elements.sidebarAccountProfile.hidden = false;
        if (elements.sidebarAccountLogin) elements.sidebarAccountLogin.hidden = true;
        if (elements.sidebarAccountLogout) elements.sidebarAccountLogout.hidden = false;
        if (elements.sidebarDeveloperTools) elements.sidebarDeveloperTools.hidden = false;
        if (elements.sidebarAccountAdmin) elements.sidebarAccountAdmin.hidden = !isAdmin;

        if (elements.profileAvatar) {
            elements.profileAvatar.textContent = state.user.username.charAt(0).toUpperCase();
        }
        if (elements.profileUsername) {
            elements.profileUsername.textContent = state.user.username;
        }
        if (elements.profileRole) {
            if (state.user.role === 'admin') {
                elements.profileRole.textContent = '管理员（无使用限制）';
            } else {
                const dailyLimit = state.user.daily_limit || 10;
                const todayUsed = state.user.today_used || 0;
                const remaining = Math.max(0, dailyLimit - todayUsed);
                elements.profileRole.textContent = `普通用户 · 今日剩余 ${remaining} 次`;
            }
        }
    } else {
        if (state.health.timer) {
            window.clearInterval(state.health.timer);
            state.health.timer = null;
        }
        document.getElementById('healthMonitor').hidden = true;
        elements.headerRight.replaceChildren();
        if (elements.sidebarAccountAvatar) elements.sidebarAccountAvatar.textContent = 'D';
        if (elements.sidebarAccountName) elements.sidebarAccountName.textContent = '未登录';
        if (elements.sidebarAccountMeta) elements.sidebarAccountMeta.textContent = '登录以同步数据';
        if (elements.sidebarAccountProfile) elements.sidebarAccountProfile.hidden = true;
        if (elements.sidebarAccountLogin) elements.sidebarAccountLogin.hidden = false;
        if (elements.sidebarAccountLogout) elements.sidebarAccountLogout.hidden = true;
        if (elements.sidebarDeveloperTools) elements.sidebarDeveloperTools.hidden = true;
        if (elements.sidebarAccountAdmin) elements.sidebarAccountAdmin.hidden = true;

        if (elements.profileAvatar) {
            elements.profileAvatar.textContent = 'D';
        }
        if (elements.profileUsername) {
            elements.profileUsername.textContent = '未登录';
        }
        if (elements.profileRole) {
            elements.profileRole.textContent = '点击登录';
        }
    }
}

// 显示登录/注册模态框
function showAuthModal() {
    elements.authModal.classList.add('active');
}

// 隐藏模态框
function hideAuthModal() {
    elements.authModal.classList.remove('active');
    elements.usernameInput.value = '';
    elements.passwordInput.value = '';
    elements.authForm.hidden = false;
    elements.switchMode.parentElement.hidden = false;
}

// 切换登录/注册模式
function toggleAuthMode() {
    if (state.authProvider === 'keycloak') { fetch(`${API_BASE}/auth/register`,{method:'POST'}).then(response=>response.ok?response.json():Promise.reject()).then(data=>window.location.assign(data.registration_url)).catch(()=>alert('无法打开 Keycloak 注册页面')); return; }
    isLoginMode = !isLoginMode;
    if (isLoginMode) {
        setAuthMode();
        return;
    }
    setAuthMode();
}

function setAuthMode() {
    if (state.authProvider === 'keycloak') {
        isLoginMode = true;
        elements.passwordInput.minLength = 0;
        elements.passwordHint.hidden = true;
        elements.modalTitle.textContent = '登录';
        elements.authSubmit.textContent = '登录';
        elements.switchText.textContent = '没有账号？';
        elements.switchMode.textContent = '注册';
        elements.forgotPasswordLink.hidden = false;
        elements.authForm.hidden = false;
        elements.switchMode.parentElement.hidden = false;
        return;
    }
    elements.passwordInput.minLength = isLoginMode ? 0 : 6;
    elements.passwordHint.hidden = isLoginMode;
    elements.modalTitle.textContent = isLoginMode ? '登录' : '注册';
    elements.authSubmit.textContent = isLoginMode ? '登录' : '注册并登录';
    elements.switchText.textContent = isLoginMode ? '没有账号？' : '已有账号？';
    elements.switchMode.textContent = isLoginMode ? '注册' : '登录';
    elements.forgotPasswordLink.hidden = true;
    elements.authForm.hidden = false;
    elements.switchMode.parentElement.hidden = false;
}

// 登录/注册
async function handleAuthSubmit(e) {
    e.preventDefault();
    const username = elements.usernameInput.value;
    const password = elements.passwordInput.value;

    if (!isLoginMode && password.length < 6) {
        alert('注册密码至少需要 6 位');
        elements.passwordInput.focus();
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/auth/${isLoginMode ? 'login' : 'register'}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });

        if (response.ok) {
            const data = await response.json();
            localStorage.setItem('access_token', data.access_token);
            if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token);
            state.user = data.user;
            scheduleTokenRefresh();
            updateUserUI();
            loadConversationHistory();
            hideAuthModal();
        } else {
            let errorMsg = '操作失败';
            if (response.status === 401) {
                errorMsg = '用户名或密码错误';
            } else if (response.status === 400 || response.status === 422) {
                try {
                    const error = await response.json();
                    const rawMessage = error?.error?.raw_message || error?.detail || '';
                    errorMsg = !isLoginMode && /password|密码/i.test(rawMessage)
                        ? '注册密码至少需要 6 位'
                        : (rawMessage || error?.error?.message || error.detail || '请求数据无效，请检查输入');
                } catch (e) {
                    errorMsg = '请求数据无效，请检查输入';
                }
            } else if (response.status === 403) {
                errorMsg = '账号已被锁定，请联系管理员';
            } else if (response.status === 500) {
                errorMsg = '服务器内部错误，请稍后重试';
            } else {
                try {
                    const error = await response.json();
                    errorMsg = error.detail || '操作失败';
                } catch (e) {
                    errorMsg = '操作失败（错误码：' + response.status + '）';
                }
            }
            alert(errorMsg);
        }
    } catch (e) {
        console.error('Network error:', e);
        let errorMsg = '网络错误，请稍后重试';
        
        if (e.name === 'TypeError' && e.message.includes('fetch')) {
            errorMsg = '无法连接到服务器，请检查服务是否启动';
        } else if (e.name === 'AbortError') {
            errorMsg = '请求超时，请稍后重试';
        } else if (!navigator.onLine) {
            errorMsg = '网络连接已断开，请检查网络';
        }
        
        alert(errorMsg);
    }
}

// 退出登录
async function logout() {
    try {
        await fetch(`${API_BASE}/auth/logout`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({ refresh_token: localStorage.getItem('refresh_token') })
        });
    } catch (e) {}
    
    clearAuthSession();
    state.user = null;
    state.currentConversationId = null;
    state.messages = [];
    state.currentProcessCard = null;
    state.currentWebAgents = [];
    state.currentIoaAgents = [];
    resetAgentDiscoveryPanels();
    state.agentResultCards = {};
    state.agentResultEvents = {};
    clearActiveAgentInteraction();
    state.agentInteractionHistory = {};
    state.agentInteractionState = {};
    state.rawAgentResultCopyTexts = {};
    state.hasRawAgentResultsInCurrentRun = false;
    state.selectedIoaAgents.clear();
    resetAgentAppPages();
    closeAgentCatalogModal();
    clearDynamicActionElement(elements.messagesContainer);
    dynamicActionPayloads.clear();
    updateUserUI();
    renderWelcome();
}

// 清空对话
async function clearChat() {
    if (!confirm('确定要清空当前对话区吗？清空后无法恢复')) {
        return;
    }
    const interaction = state.activeAgentInteraction;
    if (interaction) {
        try {
            await cancelAgentInteraction(interaction);
            markAgentInteractionCancelled(interaction);
        } catch (error) {
            console.warn('Unable to cancel active AIP task while clearing chat:', error);
        }
    }
    performClearChat();
}

// 实际执行清空对话区的状态重置与 UI 重渲染（不含确认弹窗）
function performClearChat() {
    closeAgentCatalogModal();
    clearDynamicActionElement(elements.messagesContainer);
    dynamicActionPayloads.clear();
    state.currentConversationId = null;
    state.messages = [];
    state.selectedIoaAgents.clear();
    state.currentWebAgents = [];
    state.currentIoaAgents = [];
    resetAgentDiscoveryPanels();
    state.currentProcessCard = null;
    state.agentResultCards = {};
    state.agentResultEvents = {};
    clearActiveAgentInteraction();
    state.agentInteractionHistory = {};
    state.agentInteractionState = {};
    state.rawAgentResultCopyTexts = {};
    state.hasRawAgentResultsInCurrentRun = false;
    state.messageResources = {};
    state.messageResourceSequence = 0;
    state.messageImageLightboxTrigger = null;
    clearAgentStatusPanel();
    resetAgentAppPages();
    currentStreamingMessage = null;
    currentStreamingMeta = null;
    renderWelcome();
}

function renderConversationHistory() {
    if (!elements.recentConversations) return;
    const sessions = Array.isArray(state.conversations) ? state.conversations : [];
    if (!sessions.length) {
        elements.recentConversations.className = 'recent-conversations-empty';
        elements.recentConversations.innerHTML = '<p>暂无最近对话</p><span>你的对话记录会显示在这里</span>';
        return;
    }
    elements.recentConversations.className = 'recent-conversations-list';
    elements.recentConversations.innerHTML = sessions.map(item => {
        const runtime = state.conversationRuntimes[String(item.id)];
        const activity = runtime?.loading ? runtime.activity : (item.activity_state || runtime?.activity || 'idle');
        const activityLabel = activity === 'running' ? '任务正在执行' : activity === 'attention' ? '任务已完成或等待你的操作' : '';
        const indicator = activity === 'idle' ? '' : `<span class="conversation-status-indicator is-${escapeHTML(activity)}" role="status" aria-label="${escapeHTML(activityLabel)}"></span>`;
        const itemId = String(item.id);
        const active = itemId === String(state.currentConversationId);
        const confirmingDelete = itemId === String(state.historyDeleteConfirmationId);
        const title = escapeHTML(item.title || '新对话');
        const deleteControl = confirmingDelete
            ? `<div class="recent-conversation-delete-confirm" role="group" aria-label="确认删除对话"><span>删除此对话？</span><button type="button" data-history-delete-cancel="${escapeHTML(itemId)}">取消</button><button type="button" data-history-delete-confirm="${escapeHTML(itemId)}">删除</button></div>`
            : `<button type="button" class="recent-conversation-delete-button" data-history-delete-request="${escapeHTML(itemId)}" aria-label="删除对话：${title}" title="删除对话"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6m4-6v6M9 7l1-2h4l1 2m-9 0 1 13h10l1-13"/></svg></button>`;
        return `<div class="recent-conversation-entry ${active ? 'is-active' : ''} ${confirmingDelete ? 'is-confirming-delete' : ''}"><button type="button" class="recent-conversation-item" data-conversation-id="${escapeHTML(itemId)}"><span class="recent-conversation-title">${title}</span>${indicator}</button>${deleteControl}</div>`;
    }).join('');
}

async function loadConversationHistory() {
    if (!state.user || !elements.recentConversations) return;
    try {
        const response = await fetch(`${API_BASE}/sessions`, { headers: getAuthHeaders() });
        if (!response.ok) throw await createHttpError(response, '读取历史对话');
        const data = await response.json();
        state.conversations = Array.isArray(data.sessions) ? data.sessions : [];
        renderConversationHistory();
    } catch (error) {
        console.warn('[Dingdang] 加载历史对话失败:', error);
    }
}

async function removeConversationHistory(conversationId) {
    const runtimeKey = conversationRuntimeKey(conversationId);
    if (!runtimeKey) return;
    const runtime = state.conversationRuntimes[runtimeKey];
    const confirmButton = elements.recentConversations?.querySelector(`[data-history-delete-confirm="${CSS.escape(runtimeKey)}"]`);
    if (confirmButton) confirmButton.disabled = true;
    try {
        const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(conversationId)}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        if (!response.ok) throw await createHttpError(response, '删除历史对话');

        runtime?.controller?.abort();
        const deletingCurrentConversation = String(state.currentConversationId) === runtimeKey;
        state.conversations = state.conversations.filter(item => String(item.id) !== runtimeKey);
        state.historyDeleteConfirmationId = null;
        delete state.conversationRuntimes[runtimeKey];
        if (deletingCurrentConversation) {
            performClearChat();
            state.currentConversationId = null;
            state.isLoading = false;
            state.activeRequestController = null;
            setComposerTaskRunning(false);
            elements.messageInput?.focus();
        }
        renderConversationHistory();
    } catch (error) {
        if (confirmButton) confirmButton.disabled = false;
        addErrorMessage(`删除历史对话失败：${error.message || '请稍后重试'}`);
    }
}

function startNewConversation() {
    performClearChat();
    state.currentConversationId = null;
    renderConversationHistory();
    elements.messageInput?.focus();
}

function adoptConversationId(conversationId, { activate = true } = {}) {
    if (!conversationId) return;
    const changed = String(conversationId) !== String(state.currentConversationId);
    if (activate) state.currentConversationId = conversationId;
    getConversationRuntime(conversationId);
    if (activate && state.currentProcessCard && !state.currentProcessCard.conversationId) {
        state.currentProcessCard.conversationId = conversationId;
    }
    // 后端在首条有效输入时创建会话；一拿到真实 ID 就刷新侧栏，避免用户切换后才看到它。
    if (changed) void loadConversationHistory();
}

function buildConversationReplayTimeline(session) {
    const toTimestamp = (value, fallback) => {
        const timestamp = Date.parse(value || '');
        return Number.isFinite(timestamp) ? timestamp : fallback;
    };
    const messages = Array.isArray(session?.messages) ? session.messages : [];
    const events = Array.isArray(session?.events) ? session.events : [];
    const explicitSequence = value => {
        const sequence = Number(value?.timeline_seq ?? value?.event_seq);
        return Number.isFinite(sequence) && sequence > 0 ? sequence : null;
    };
    return [
        ...messages.map((message, index) => ({
            kind: 'message',
            value: message,
            timestamp: explicitSequence(message) == null ? toTimestamp(message.created_at, index) : 0,
            sequence: explicitSequence(message) ?? (Number(message.id) || index)
        })),
        ...events.map((stored, index) => ({
            kind: 'event',
            value: stored,
            timestamp: explicitSequence(stored) == null ? toTimestamp(stored.created_at, messages.length + index) : 0,
            sequence: explicitSequence(stored) ?? (Number(stored.event_seq) || index)
        }))
    ].sort((left, right) => (
        left.timestamp - right.timestamp
        || left.sequence - right.sequence
        || (left.kind === 'event' ? -1 : 1)
    ));
}

// 选择事件既可能在本轮助手文本之后（常规路径），也可能在之前（兜底路径）。
// 回放时只把“回复前”的选择暂存到同一轮回复之后，不能把所有选择拖到下一轮。
function createReplayTurnOrderQueue(renderSelection) {
    let lastMessageRole = '';
    const pendingSelections = [];
    const flush = () => {
        pendingSelections.splice(0).forEach(renderSelection);
    };

    return {
        recordMessage(message) {
            lastMessageRole = message?.role || '';
            if (lastMessageRole === 'assistant') flush();
        },
        recordSelection(event) {
            if (lastMessageRole === 'assistant') {
                renderSelection(event);
            } else {
                pendingSelections.push(event);
            }
        },
        flush
    };
}

function completionInteractionKeys(event) {
    return ["task_id", "taskId", "run_id", "runId", "collaboration_task_id", "collaborationTaskId"]
        .map(key => String(event?.[key] || "").trim())
        .filter(Boolean);
}

function confirmedCompletionInteractionKeys(session) {
    const confirmedKeys = new Set();
    (session?.events || []).forEach(stored => {
        const event = { ...(stored?.payload || {}), type: stored?.type };
        const confirmed = event.type === "execution_complete"
            && (event.supersedes_confirmation === true || event.message === "用户已确认候选方案，协作完成。");
        if (confirmed) completionInteractionKeys(event).forEach(key => confirmedKeys.add(key));
    });
    return confirmedKeys;
}

function isSupersededCompletionCandidate(event, confirmedKeys) {
    if (!confirmedKeys?.size) return false;
    if (event?.type !== "execution_awaiting_completion" && event?.type !== "agent_result_card") return false;
    return completionInteractionKeys(event).some(key => confirmedKeys.has(key));
}

function removeConfirmedCompletionArtifacts(pending) {
    const confirmedKeys = new Set(completionInteractionKeys(pending));
    if (!confirmedKeys.size) return;
    Object.entries(state.agentResultEvents || {}).forEach(([cardRef, event]) => {
        if (!isSupersededCompletionCandidate(event, confirmedKeys)) return;
        removeDynamicActionElement(document.getElementById(cardRef));
        removeDynamicActionElement(document.getElementById(cardRef.replace(/^agent-result-/, "agent-raw-result-")));
        delete state.agentResultEvents[cardRef];
        delete state.agentResultCards[cardRef];
        delete state.rawAgentResultCopyTexts[cardRef.replace(/^agent-result-/, "agent-raw-result-")];
    });
}

// 交互事件本身是审计日志的一部分。历史回放不再把过期的“待输入/待确认”
// 当成当前控制器恢复，但必须把当时的问题和候选内容保留在消息时间线中。
function getHistoricalInteractionReplayMessage(event) {
    const agentName = String(event?.agent_name || event?.agent || '叮当');
    if (event?.type === 'execution_waiting_input') {
        const question = String(event.content || event.question || event.message || '请补充完成任务所需的信息。');
        return {
            role: 'assistant',
            content: `${agentName} 需要你补充信息：\n\n${question}`
        };
    }
    if (event?.type === 'execution_awaiting_completion') {
        const parts = [
            `${agentName} 提交了候选方案，等待你的确认。`,
            event.content ? `候选方案：\n\n${event.content}` : '',
            event.question ? `需要确认：\n\n${event.question}` : '',
            !event.content && !event.question && event.message ? event.message : ''
        ].filter(Boolean);
        return { role: 'assistant', content: parts.join('\n\n') };
    }
    return null;
}

// 旧版本没有把叮当协作续办的用户回答单独写入 messages，但随后协作运行
// 事件的 goal 仍保留了“用户回答：”段。只在消息表中确实不存在时恢复它。
function getLegacyContinuationUserReply(event) {
    if (event?.type !== 'collaboration_run') return '';
    const goal = String(event.goal || '');
    const match = goal.match(/(?:^|\n)用户回答：([\s\S]+)$/);
    return match ? match[1].trim() : '';
}

async function replayConversation(session) {
    const persistedUserContents = new Set(
        (session.messages || [])
            .filter(message => message?.role === 'user')
            .map(message => String(message.content || '').trim())
            .filter(Boolean)
    );
    const confirmedKeys = confirmedCompletionInteractionKeys(session);
    const replaySelectionOrder = createReplayTurnOrderQueue(event => {
        addAgentSelectionPanel(
            event.web_agents || [],
            event.ioa_agents || [],
            event.message,
            event.conversation_id,
            normalizeRecommendationEnvelope(event)
        );
    });

    for (const item of buildConversationReplayTimeline(session)) {
        if (item.kind === 'message') {
            const message = item.value;
            addMessageToUI(message);
            state.messages.push({ role: message.role, content: message.content });
            replaySelectionOrder.recordMessage(message);
            continue;
        }
        const stored = item.value;
        const event = { ...(stored.payload || {}), type: stored.type };
        if (isSupersededCompletionCandidate(event, confirmedKeys)) continue;
        if (event.type === 'agent_selection') {
            replaySelectionOrder.recordSelection(event);
        } else if (['execution_waiting_input', 'execution_awaiting_completion'].includes(event.type)) {
            const activeCompletionSequence = Number(session.active_interaction?.event_seq || 0);
            const isActiveCompletion = event.type === 'execution_awaiting_completion'
                && Number(stored.event_seq || 0) === activeCompletionSequence;
            if (!isActiveCompletion) {
                const historicalPrompt = getHistoricalInteractionReplayMessage(event);
                if (historicalPrompt) {
                    addMessageToUI(historicalPrompt);
                    state.messages.push(historicalPrompt);
                    replaySelectionOrder.recordMessage(historicalPrompt);
                }
            }
        } else {
            const legacyUserReply = getLegacyContinuationUserReply(event);
            if (legacyUserReply && !persistedUserContents.has(legacyUserReply)) {
                const recoveredReply = { role: 'user', content: legacyUserReply };
                addMessageToUI(recoveredReply);
                state.messages.push(recoveredReply);
                replaySelectionOrder.recordMessage(recoveredReply);
                persistedUserContents.add(legacyUserReply);
            }
            await handleExecutionEvent(event, session.id, { replay: true });
        }
    }
    replaySelectionOrder.flush();
    if (session.active_interaction) {
        if (String(session.active_interaction.state || '').includes('awaiting-completion')) {
            await handleExecutionEvent({
                ...session.active_interaction,
                type: 'execution_awaiting_completion',
                conversation_id: session.id,
                content: session.active_interaction.content || session.active_interaction.question || '',
                question: session.active_interaction.question || '',
                message: session.active_interaction.message || ''
            }, session.id);
        } else {
            restoreActiveAgentInteraction(session.active_interaction);
        }
    }
    if (!session.active_interaction) clearActiveAgentInteraction();
}

function replayedConversationActivity(session) {
    const events = Array.isArray(session?.events) ? session.events : [];
    const waiting = events.some(stored => {
        const payload = stored?.payload || {};
        const status = String(payload.state || payload.status || '').toLowerCase();
        return status.includes('awaiting') || status.includes('waiting');
    });
    return waiting ? 'attention' : 'idle';
}

async function openConversation(conversationId) {
    if (!conversationId) return;
    try {
        const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(conversationId)}`, { headers: getAuthHeaders() });
        if (!response.ok) throw await createHttpError(response, '打开历史对话');
        const session = await response.json();
        performClearChat();
        state.currentConversationId = session.id;
        const runtime = getConversationRuntime(session.id);
        state.isLoading = runtime.loading;
        state.activeRequestController = runtime.controller;
        setComposerTaskRunning(runtime.loading);
        await replayConversation(session);
        if (!runtime.loading) {
            setConversationActivity(session.id, session.activity_state || 'idle');
        }
        await loadConversationHistory();
        scrollToBottom();
    } catch (error) {
        addErrorMessage(`打开历史对话失败：${error.message || '请稍后重试'}`);
    }
}

// The conversation and its live execution dashboard describe the same run.
// Clear both atomically so stale tasks cannot look active after a reset.
function clearAgentStatusPanel() {
    if (statusUpdateInterval) {
        clearInterval(statusUpdateInterval);
        statusUpdateInterval = null;
    }
    executionStartTime = null;
    agentPollCounts = {};
    agentElapsedTimes = {};
    state.collaborationRuns = Object.create(null);
    state.pendingSecretaryCollaboration = null;
    state.pendingSecretaryCompletion = null;
    state.pendingAgentCompletion = null;
    document.querySelector('.collaboration-detail-modal')?.remove();
    const panelContent = document.querySelector('.status-panel .panel-content');
    if (panelContent) panelContent.innerHTML = '<div class="empty-state">暂无活动</div>';
}

// 渲染欢迎界面
function renderWelcome() {
    replaceDynamicActionHTML(elements.messagesContainer, `
        <div class="welcome-message">
            <div class="welcome-avatar">
                <img src="dingdang.png" alt="叮当">
            </div>
            <h2>你好！我是叮当</h2>
            <p>你的智能体浏览器助手。我可以帮你找到合适的智能体来完成各种任务。</p>
        </div>
    `);
}

// 渲染消息
function renderMessages() {
    if (state.messages.length === 0) {
        renderWelcome();
        return;
    }

    let html = '';
    let conversationId = null;
    resetAgentDiscoveryPanels();
    
    state.messages.forEach(msg => {
        html += createMessageHTML(msg);
        
        const metadata = msg.message_metadata || {};
        const webAgents = metadata.web_agents || [];
        const ioaAgents = metadata.ioa_agents || [];
        
        if ((webAgents.length > 0 || ioaAgents.length > 0) && msg.conversation_id) {
            conversationId = msg.conversation_id;
            const panel = registerAgentDiscoveryPanel(webAgents, ioaAgents, msg.conversation_id, metadata);
            html += createAgentSelectionHTML(panel);
        }
    });
    
    replaceDynamicActionHTML(elements.messagesContainer, html);
    
    setupAgentCheckboxListeners();
    scrollToBottom();
}

// 汇总 Agent 自身能力声明；不混入 assigned_task，避免“被分配了天气任务”的通用 Agent 被误判成天气应用。
function getAgentCapabilityText(agent) {
    const skillsText = (Array.isArray(agent?.skills) ? agent.skills : []).map(skill => {
        const tags = Array.isArray(skill?.tags) ? skill.tags.join(' ') : '';
        const examples = Array.isArray(skill?.examples) ? skill.examples.join(' ') : '';
        return [skill?.id, skill?.name, skill?.description, tags, examples].filter(Boolean).join(' ');
    }).join('\n');
    const capabilities = Array.isArray(agent?.capabilities) ? agent.capabilities.join(' ') : '';
    return [
        agent?.name,
        agent?.description,
        agent?.aic,
        agent?.endpoint || agent?.url,
        agent?.skill_id || agent?.skillId,
        capabilities,
        skillsText
    ].filter(Boolean).join('\n');
}

// 保持 Discovery 返回的排序；叮当不按智能体名称、厂商或本地地址额外加权。
function selectDefaultIoaAgent(agents) {
    const candidates = Array.isArray(agents) ? agents.filter(Boolean) : [];
    return candidates[0] || null;
}

function shouldDefaultSelectIoaAgent(agent, index = 0, agents = state.currentIoaAgents) {
    const selected = selectDefaultIoaAgent(agents);
    if (!selected) return false;
    if (selected.aic && agent?.aic) return selected.aic === agent.aic;
    return selected.name === agent?.name;
}

function resetAgentDiscoveryPanels() {
    state.agentDiscoveryPanels = {};
    state.agentDiscoveryPanelSequence = 0;
    state.activeAgentDiscoveryPanelId = '';
}

function recommendationSkillKeys(agent) {
    const skills = Array.isArray(agent?.skills) ? agent.skills : [];
    return new Set(skills.flatMap(skill => [skill?.id, skill?.skill_id, skill?.name]
        .filter(Boolean)
        .map(value => String(value).trim())));
}

function normalizeRecommendedCombination(recommendation, ioaAgents) {
    const rawEntries = Array.isArray(recommendation?.selected_combination)
        ? recommendation.selected_combination
        : (Array.isArray(recommendation?.recommended_combination)
            ? recommendation.recommended_combination
            : (Array.isArray(recommendation?.executable_combination) ? recommendation.executable_combination : []));
    const candidates = Array.isArray(ioaAgents) ? ioaAgents : [];
    const plannedTasks = Array.isArray(recommendation?.planned_tasks) ? recommendation.planned_tasks : [];
    return rawEntries.filter(entry => entry && typeof entry === 'object').map(entry => {
        const aic = String(entry.aic || '').trim();
        const skillId = String(entry.skill_id || entry.skillId || '').trim();
        const aicCandidates = candidates.filter(agent => !aic || String(agent?.aic || '').trim() === aic);
        const exactMatches = skillId
            ? aicCandidates.filter(agent => recommendationSkillKeys(agent).has(skillId))
            : aicCandidates;
        const agent = exactMatches.length === 1
            ? exactMatches[0]
            : (!skillId && aicCandidates.length === 1 ? aicCandidates[0] : null);
        const plannedTask = plannedTasks.find(task => (
            String(task?.aic || '').trim() === aic
            && String(task?.skill_id || task?.skillId || '').trim() === skillId
        ));
        const assignedTask = String(
            entry.assigned_task
            || plannedTask?.description
            || plannedTask?.assigned_task
            || entry.reason
            || recommendation?.intent_summary
            || ''
        ).trim();
        const executable = Boolean(
            agent
            && entry.executable !== false
            && String(entry.agent_type || 'ioa').toLowerCase() === 'ioa'
        );
        return {
            ...entry,
            aic,
            skill_id: skillId,
            agent,
            agentName: String(entry.agent_name || agent?.name || '').trim(),
            assignedTask,
            executable
        };
    });
}

function normalizeRecommendationEnvelope(event) {
    return {
        intent_summary: event?.intent_summary || '',
        selected_combination: event?.recommended_combination || event?.selected_combination || [],
        executable_combination: event?.executable_combination || [],
        planned_tasks: event?.planned_tasks || [],
        coverage_analysis: event?.coverage_analysis || [],
        missing_capabilities: event?.missing_capabilities || [],
        discovery_rounds: event?.discovery_rounds || 0,
        warnings: event?.discovery_warnings || []
    };
}

function getRecommendedEntries(panel, executableOnly = false) {
    const entries = Array.isArray(panel?.recommendedEntries) ? panel.recommendedEntries : [];
    return executableOnly ? entries.filter(entry => entry.executable) : entries;
}

function getRecommendedEntryForAgent(panel, agent) {
    return getRecommendedEntries(panel).find(entry => entry.agent === agent || (
        entry.executable
        && entry.agentName
        && entry.agentName === agent?.name
    )) || null;
}

function registerAgentDiscoveryPanel(webAgents, ioaAgents, conversationId, recommendation = null) {
    const panelId = `agent-discovery-${++state.agentDiscoveryPanelSequence}`;
    const panel = {
        id: panelId,
        conversationId: conversationId || ensureConversationId('explore'),
        webAgents: (Array.isArray(webAgents) ? webAgents : []).map(agent => ({ ...agent })),
        ioaAgents: (Array.isArray(ioaAgents) ? ioaAgents : []).map(agent => ({ ...agent })),
        recommendation: recommendation || null,
        recommendedEntries: [],
        selectedIoaAgents: new Set(),
        queryText: state.currentTaskIntent.queryText || '',
        attachmentIds: Array.from(state.currentTaskIntent.attachmentIds || [])
    };
    panel.recommendedEntries = normalizeRecommendedCombination(panel.recommendation, panel.ioaAgents);
    const recommendedNames = new Set(
        getRecommendedEntries(panel, true).map(entry => entry.agent?.name).filter(Boolean)
    );
    if (recommendedNames.size > 0) {
        recommendedNames.forEach(name => panel.selectedIoaAgents.add(name));
    }
    panel.ioaAgents.forEach((agent, index) => {
        if (recommendedNames.size === 0 && shouldDefaultSelectIoaAgent(agent, index, panel.ioaAgents)) {
            panel.selectedIoaAgents.add(agent.name);
        }
    });
    state.agentDiscoveryPanels[panelId] = panel;
    activateAgentDiscoveryPanel(panel);
    return panel;
}

function getAgentDiscoveryPanel(panelId) {
    const resolvedId = panelId || state.activeAgentDiscoveryPanelId;
    return resolvedId ? state.agentDiscoveryPanels[resolvedId] || null : null;
}

function activateAgentDiscoveryPanel(panel) {
    if (!panel) return null;
    state.activeAgentDiscoveryPanelId = panel.id;
    state.currentWebAgents = panel.webAgents;
    state.currentIoaAgents = panel.ioaAgents;
    state.currentConversationId = panel.conversationId;
    state.selectedIoaAgents = panel.selectedIoaAgents;
    return panel;
}

function renderRecommendedCombinationHTML(panel) {
    const entries = getRecommendedEntries(panel);
    if (entries.length === 0) return '';
    const executableEntries = entries.filter(entry => entry.executable);
    const coverage = Array.isArray(panel?.recommendation?.coverage_analysis)
        ? panel.recommendation.coverage_analysis.filter(item => item && item.capability)
        : [];
    const missing = Array.isArray(panel?.recommendation?.missing_capabilities)
        ? panel.recommendation.missing_capabilities.filter(Boolean)
        : [];
    const countLabel = executableEntries.length > 0
        ? `${executableEntries.length} 个可执行智能体`
        : '当前没有可直接执行的推荐智能体';
    const coverageHTML = coverage.length > 0
        ? `<div class="recommended-coverage-list">${coverage.slice(0, 5).map(item => `
            <span class="recommended-coverage-item ${item.covered ? 'is-covered' : 'is-missing'}">
                <span aria-hidden="true">${item.covered ? '✓' : '!'}</span>${escapeHTML(item.capability)}
            </span>`).join('')}</div>`
        : '';
    const missingHTML = missing.length > 0
        ? `<div class="recommended-missing" role="status">尚未覆盖：${escapeHTML(missing.join('、'))}</div>`
        : '';
    const actionHTML = executableEntries.length > 0
        ? `<button class="btn-confirm-recommended" type="button" ${renderDynamicActionAttributes('confirm-recommended-agents', { panelId: panel.id })}>确认推荐组合并执行（${executableEntries.length}）</button>`
        : `<span class="recommended-unavailable">可手动选择下方候选智能体执行</span>`;
    return `
        <section class="agent-recommendation-panel" aria-label="推荐组合">
            <header class="recommended-combination-header">
                <div>
                    <span class="recommended-combination-kicker">叮当推荐组合</span>
                    <strong>按能力分工协作</strong>
                </div>
                <span class="recommended-combination-count">${escapeHTML(countLabel)}</span>
            </header>
            <p class="recommended-combination-summary">${escapeHTML(panel.recommendation?.intent_summary || '根据当前需求选择了一组更适合协作的智能体。')}</p>
            <div class="recommended-combination-list">
                ${entries.map(entry => `
                    <article class="recommended-agent-item ${entry.executable ? 'recommended-executable' : 'recommended-not-executable'}">
                        <div class="recommended-agent-index" aria-hidden="true">${entry.executable ? '✦' : '·'}</div>
                        <div class="recommended-agent-content">
                            <div class="recommended-agent-heading">
                                <strong>${escapeHTML(entry.agentName || '推荐智能体待匹配')}</strong>
                                <span class="recommended-agent-role">${escapeHTML(entry.role || 'primary')}</span>
                            </div>
                            <div class="recommended-agent-skill">${escapeHTML(entry.skill_id || '通用能力')}</div>
                            ${entry.assignedTask ? `<div class="recommended-agent-task">${escapeHTML(entry.assignedTask)}</div>` : ''}
                            ${entry.reason ? `<div class="recommended-agent-reason">${escapeHTML(entry.reason)}</div>` : ''}
                            ${entry.executable ? '' : '<div class="recommended-agent-unavailable">该推荐项不是当前可直接调用的 IOA 智能体</div>'}
                        </div>
                    </article>
                `).join('')}
            </div>
            ${coverageHTML}
            ${missingHTML}
            <footer class="recommended-combination-footer">
                ${actionHTML}
                <span class="recommended-confirm-hint">发现结果不会自动执行，点击按钮后才会开始协作。</span>
            </footer>
        </section>
    `;
}

// 创建智能体选择面板 HTML
function createAgentSelectionHTML(panel, discoveryMessage = '') {
    const webAgents = panel?.webAgents || [];
    const ioaAgents = panel?.ioaAgents || [];
    const conversationId = panel?.conversationId || '';
    const panelId = panel?.id || '';
    const hasWebAgents = webAgents && webAgents.length > 0;
    const hasIoaAgents = ioaAgents && ioaAgents.length > 0;
    const previewCount = 3;

    const totalCount = webAgents.length + ioaAgents.length;
    let html = `
        <section class="agent-selection-container inline-agent-discovery" id="${escapeHTMLAttribute(panelId)}" aria-label="智能体发现结果" data-discovery-id="${escapeHTMLAttribute(panelId)}" data-conversation-id="${escapeHTMLAttribute(conversationId)}">
            <header class="agent-selection-header">
                <div class="agent-selection-heading">
                    <span class="agent-selection-icon" aria-hidden="true">✦</span>
                    <div>
                        <span class="agent-selection-kicker">能力匹配</span>
                        <strong>${escapeHTML(discoveryMessage || '为你找到以下智能体')}</strong>
                    </div>
                </div>
                <span class="agent-selection-total">${totalCount} 项</span>
            </header>
    `;

    html += renderRecommendedCombinationHTML(panel);
    html += `<div class="agent-selection-columns">`;

    if (hasWebAgents) {
        html += `
            <div class="agent-column web-agents">
                <div class="agent-column-header">
                    <span class="column-title">浏览与工具</span>
                    <span class="column-count">${webAgents.length}</span>
                </div>
                <div class="agent-list">
                    ${webAgents.slice(0, previewCount).map((agent, index) => `
                        <div class="agent-item" data-agent-name="${escapeHTMLAttribute(agent.name)}">
                            <div class="agent-info">
                                <div class="agent-name">${escapeHTML(agent.name)}</div>
                                <div class="agent-description">${escapeHTML(agent.description || '')}</div>
                            </div>
                            <button class="btn-open-agent" type="button" ${renderDynamicActionAttributes('open-discovery-web', { panelId, index })}>打开</button>
                        </div>
                    `).join('')}
                </div>
                <button class="btn-view-more-agents" type="button" ${renderDynamicActionAttributes('show-agent-catalog', { kind: 'web', panelId })}>查看更多（${webAgents.length}）</button>
            </div>
        `;
    }

    if (hasIoaAgents) {
        html += `
            <div class="agent-column ioa-agents">
                <div class="agent-column-header">
                    <span class="column-title">可调用智能体</span>
                    <span class="column-count">${ioaAgents.length}</span>
                </div>
                <div class="agent-list">
                    ${ioaAgents.slice(0, previewCount).map(agent => {
                        const deployment = getAgentDeploymentScope({
                            ...agent,
                            agent_name: agent.name,
                            endpoint: agent.endpoint || agent.url
                        });
                        const recommendationEntry = getRecommendedEntryForAgent(panel, agent);
                        const recommendedClass = recommendationEntry?.executable ? ' recommended-executable' : '';
                        const recommendedBadge = recommendationEntry?.executable
                            ? '<span class="recommended-agent-badge">推荐</span>'
                            : '';
                        return `
                            <div class="agent-item ioa-item${recommendedClass}" data-agent-name="${escapeHTMLAttribute(agent.name)}" data-agent-scope="${escapeHTMLAttribute(deployment.scope)}">
                                <div class="agent-checkbox-wrapper">
                                    <input type="checkbox" class="agent-checkbox" value="${escapeHTMLAttribute(agent.name)}" ${panel.selectedIoaAgents.has(agent.name) ? 'checked' : ''}>
                                </div>
                                <div class="agent-info">
                                    <div class="agent-name-row">
                                        <div class="agent-name">${escapeHTML(agent.name)}</div>
                                        ${recommendedBadge}
                                        <span class="agent-scope-badge ${deployment.scope === 'local' ? 'local' : 'cloud'}">${escapeHTML(deployment.selectionLabel)}</span>
                                    </div>
                                    <div class="agent-description">${escapeHTML(agent.description || '')}</div>
                                    <label class="agent-task agent-task-editor">
                                        <span class="task-label">任务</span>
                                        <textarea class="agent-assigned-task-input" data-agent-name="${escapeHTMLAttribute(agent.name)}" rows="2" aria-label="${escapeHTMLAttribute(agent.name)} 的调用指令" placeholder="请输入要交给智能体的指令...">${escapeHTML(agent.assigned_task || state.currentTaskIntent.queryText || '')}</textarea>
                                    </label>
                                </div>
                            </div>
                        `;
                    }).join('')}
                </div>
                <div class="agent-execute-footer">
                    <button class="btn-execute-agents" type="button" ${renderDynamicActionAttributes('execute-selected-agents', { panelId })}>执行已选智能体 (${panel.selectedIoaAgents.size})</button>
                    <button class="btn-view-more-agents" type="button" ${renderDynamicActionAttributes('show-agent-catalog', { kind: 'ioa', panelId })}>查看更多（${ioaAgents.length}）</button>
                </div>
            </div>
        `;
    }

    html += `</div>`;

    html += `</section>`;
    return html;
}

function closeAgentCatalogModal() {
    removeDynamicActionElement(document.getElementById('agent-catalog-modal'));
    agentCatalogPanelId = '';
}

let agentCatalogDraftTasks = new Map();
let agentCatalogSelectedNames = new Set();
let agentCatalogPage = 0;
let agentCatalogPanelId = '';
const AGENT_CATALOG_PAGE_SIZE = 6;

function getAgentCatalogAgents(kind, panelId = agentCatalogPanelId) {
    const panel = getAgentDiscoveryPanel(panelId);
    if (panel) return kind === 'ioa' ? panel.ioaAgents : panel.webAgents;
    return kind === 'ioa' ? state.currentIoaAgents : state.currentWebAgents;
}

function renderAgentCatalogCards(kind) {
    const agents = getAgentCatalogAgents(kind) || [];
    const start = agentCatalogPage * AGENT_CATALOG_PAGE_SIZE;
    return agents.slice(start, start + AGENT_CATALOG_PAGE_SIZE).map((agent, offset) => {
        const index = start + offset;
        const url = kind === 'ioa' ? '' : String(agent.url || '');
        const description = UI_HELPERS.getAgentDescriptionForDisplay
            ? UI_HELPERS.getAgentDescriptionForDisplay(agent.name, agent.description)
            : String(agent.description || '').trim();
        return `
            <article class="agent-catalog-card ${offset === 0 ? 'is-active' : ''}" data-agent-index="${index}" ${renderDynamicActionAttributes('show-catalog-details', { kind, index })}>
                <input class="agent-catalog-checkbox" type="checkbox" value="${escapeHTMLAttribute(agent.name)}" data-agent-url="${escapeHTMLAttribute(url)}" data-agent-catalog-checkbox ${agentCatalogSelectedNames.has(agent.name) ? 'checked' : ''}>
                <div class="agent-catalog-content"><strong>${escapeHTML(agent.name)}</strong>${description ? `<span>${escapeHTML(description)}</span>` : ''}</div>
            </article>`;
    }).join('');
}

function renderAgentCatalogPage(kind) {
    const agents = getAgentCatalogAgents(kind) || [];
    const totalPages = Math.max(1, Math.ceil(agents.length / AGENT_CATALOG_PAGE_SIZE));
    agentCatalogPage = Math.min(Math.max(0, agentCatalogPage), totalPages - 1);
    const list = document.getElementById('agent-catalog-list');
    const pagination = document.getElementById('agent-catalog-pagination');
    if (list) replaceDynamicActionHTML(list, renderAgentCatalogCards(kind));
    if (pagination) {
        replaceDynamicActionHTML(pagination, `
            <button class="agent-catalog-page-button" type="button" ${renderDynamicActionAttributes('set-agent-catalog-page', { kind, page: agentCatalogPage - 1 })} ${agentCatalogPage === 0 ? 'disabled' : ''}>上一页</button>
            <span>${agentCatalogPage + 1} / ${totalPages}</span>
            <button class="agent-catalog-page-button" type="button" ${renderDynamicActionAttributes('set-agent-catalog-page', { kind, page: agentCatalogPage + 1 })} ${agentCatalogPage >= totalPages - 1 ? 'disabled' : ''}>下一页</button>`);
    }
    showAgentCatalogDetails(kind, agentCatalogPage * AGENT_CATALOG_PAGE_SIZE);
    syncAgentCatalogSelectAllLabel(kind);
}

function setAgentCatalogPage(kind, page) {
    agentCatalogPage = page;
    renderAgentCatalogPage(kind);
}

function toggleAgentCatalogSelection(name, checked) {
    if (checked) agentCatalogSelectedNames.add(name);
    else agentCatalogSelectedNames.delete(name);
    syncAgentCatalogSelectAllLabel(document.getElementById('agent-catalog-modal')?.dataset.agentCatalogKind || '');
}

function isAgentCatalogFullySelected(kind) {
    const agents = getAgentCatalogAgents(kind) || [];
    return agents.length > 0 && agents.every(agent => agentCatalogSelectedNames.has(agent.name));
}

function syncAgentCatalogSelectAllLabel(kind) {
    const selectAllButton = document.getElementById('agent-catalog-select-all');
    if (!selectAllButton) return;
    selectAllButton.textContent = isAgentCatalogFullySelected(kind) ? '取消全选' : '全选';
}

function toggleAgentCatalogSelectAll(kind) {
    const agents = getAgentCatalogAgents(kind) || [];
    const allSelected = isAgentCatalogFullySelected(kind);
    if (allSelected) agentCatalogSelectedNames.clear();
    else agents.forEach(agent => agentCatalogSelectedNames.add(agent.name));
    renderAgentCatalogPage(kind);
}

function updateAgentCatalogTask(index, value) {
    agentCatalogDraftTasks.set(Number(index), value);
}

function showAgentCatalogDetails(kind, index) {
    const isIoa = kind === 'ioa';
    const agents = getAgentCatalogAgents(kind);
    const agent = agents?.[Number(index)];
    const panel = document.getElementById('agent-catalog-details');
    if (!agent || !panel) return;
    const description = UI_HELPERS.getAgentDescriptionForDisplay
        ? UI_HELPERS.getAgentDescriptionForDisplay(agent.name, agent.description)
        : String(agent.description || '').trim();
    document.querySelectorAll('.agent-catalog-card').forEach(card => {
        card.classList.toggle('is-active', Number(card.dataset.agentIndex) === Number(index));
    });
    const capabilities = Array.isArray(agent.capabilities) && agent.capabilities.length
        ? agent.capabilities
        : (agent.skills || []).flatMap(skill => skill?.tags || []);
    const capabilityHtml = capabilities.length
        ? capabilities.map(item => `<span>${escapeHTML(String(item))}</span>`).join('')
        : '<span>注册信息未提供能力标签</span>';
    const taskEditor = isIoa ? `
        <label class="agent-catalog-task">
            <span>调用指令</span>
            <textarea class="agent-catalog-task-input" rows="3" data-agent-catalog-task-index="${Number(index)}">${escapeHTML(agentCatalogDraftTasks.get(Number(index)) ?? agent.assigned_task ?? state.currentTaskIntent.queryText ?? '')}</textarea>
        </label>` : '';
    replaceDynamicActionHTML(panel, `
        <div class="agent-catalog-detail-heading">
            <strong>${escapeHTML(agent.name)}</strong>
            ${description ? `<span>${escapeHTML(description)}</span>` : ''}
        </div>
        <div class="agent-catalog-capabilities">${capabilityHtml}</div>
        ${taskEditor}`);
}

function applyModalTaskInstructions() {
    const panel = getAgentDiscoveryPanel(agentCatalogPanelId);
    const agents = panel?.ioaAgents || state.currentIoaAgents;
    const panelElement = panel ? document.getElementById(panel.id) : document;
    agentCatalogDraftTasks.forEach((task, index) => {
        const agent = agents[index];
        if (!agent) return;
        agent.assigned_task = task.trim();
        panelElement?.querySelectorAll(`.agent-assigned-task-input[data-agent-name="${CSS.escape(agent.name)}"]`)
            .forEach(editor => { editor.value = task; });
    });
}

function confirmIoaCatalogSelection() {
    const panelId = agentCatalogPanelId;
    const panel = getAgentDiscoveryPanel(panelId);
    const selectedNames = panel?.selectedIoaAgents || state.selectedIoaAgents;
    selectedNames.clear();
    agentCatalogSelectedNames.forEach(name => selectedNames.add(name));
    applyModalTaskInstructions();
    const panelElement = panel ? document.getElementById(panel.id) : document;
    panelElement?.querySelectorAll('.agent-checkbox').forEach(input => {
        input.checked = selectedNames.has(input.value);
    });
    updateExecuteButton(panelId);
    closeAgentCatalogModal();
    agentCatalogDraftTasks = new Map();
    executeSelectedAgents(null, { panelId });
}

async function confirmWebCatalogSelection() {
    const panel = getAgentDiscoveryPanel(agentCatalogPanelId);
    const selectedUrls = (panel?.webAgents || state.currentWebAgents || [])
        .filter(agent => agentCatalogSelectedNames.has(agent.name))
        .map(agent => agent.url)
        .filter(Boolean);
    if (selectedUrls.length === 0) {
        alert('请至少选择一个 Web 应用');
        return;
    }
    const taskInput = document.querySelector('.agent-catalog-web-task-input');
    const task = taskInput?.value.trim();
    if (task && navigator.clipboard?.writeText) {
        try {
            await navigator.clipboard.writeText(task);
        } catch (error) {
            console.warn('Could not copy Web application task instruction:', error);
        }
    }
    closeAgentCatalogModal();
    selectedUrls.forEach(url => window.open(url, '_blank', 'noopener'));
}

function showAgentCatalogModal(kind, requestedPanelId = '') {
    closeAgentCatalogModal();
    const panel = getAgentDiscoveryPanel(requestedPanelId);
    if (panel) {
        agentCatalogPanelId = panel.id;
        activateAgentDiscoveryPanel(panel);
    }
    const isIoa = kind === 'ioa';
    const agents = getAgentCatalogAgents(kind, agentCatalogPanelId);
    if (!Array.isArray(agents) || agents.length === 0) return;
    agentCatalogDraftTasks = new Map();
    agentCatalogPage = 0;
    agentCatalogSelectedNames = new Set(
        isIoa ? (panel?.selectedIoaAgents || state.selectedIoaAgents) : (agents[0] ? [agents[0].name] : [])
    );
    const defaultTask = panel?.queryText || state.currentTaskIntent.queryText || '';
    const action = isIoa
        ? `<button class="btn-execute-agents" type="button" ${renderDynamicActionAttributes('confirm-ioa-catalog', {})}>确认调用所选智能体</button>`
        : `<button class="btn-execute-agents" type="button" ${renderDynamicActionAttributes('confirm-web-catalog', {})}>确认打开所选应用</button>`;
    const modal = `
        <div id="agent-catalog-modal" class="modal-overlay agent-catalog-overlay" data-agent-catalog-kind="${escapeHTMLAttribute(kind)}" role="dialog" aria-modal="true">
            <div class="modal agent-catalog-modal">
                <div class="modal-header">
                    <h3>${isIoa ? '全部服务调用智能体' : '全部 Web 应用智能体'}</h3>
                    <button class="modal-close" type="button" ${renderDynamicActionAttributes('close-agent-catalog', {})} aria-label="关闭">×</button>
                </div>
                <p class="agent-catalog-hint">${isIoa ? '选择智能体、修改各自的调用指令后确认调用。' : '选择要打开的 Web 应用；任务指令会在确认时复制，便于粘贴到应用页面。'}</p>
                ${isIoa ? '' : `<label class="agent-catalog-web-task"><span>任务指令</span><textarea class="agent-catalog-web-task-input" rows="3">${escapeHTML(defaultTask)}</textarea></label>`}
                <div class="agent-catalog-body">
                    <div class="agent-catalog-results"><div id="agent-catalog-list" class="agent-catalog-list"></div><div id="agent-catalog-pagination" class="agent-catalog-pagination"></div></div>
                    <aside id="agent-catalog-details" class="agent-catalog-details"></aside>
                </div>
                <div class="agent-catalog-actions">
                    <button id="agent-catalog-select-all" class="agent-catalog-select-all" type="button" ${renderDynamicActionAttributes('toggle-agent-catalog-all', { kind })}>全选</button>
                    <button class="btn-secondary agent-catalog-cancel" type="button" ${renderDynamicActionAttributes('close-agent-catalog', {})}>取消</button>
                    ${action}
                </div>
            </div>
        </div>`;
    document.body.insertAdjacentHTML('beforeend', modal);
    renderAgentCatalogPage(kind);
}

function openDiscoveryWebAgent(panelId, index) {
    const panel = getAgentDiscoveryPanel(panelId);
    const agent = panel?.webAgents?.[Number(index)];
    const url = MESSAGE_RESOURCES.sanitizeResourceUrl
        ? MESSAGE_RESOURCES.sanitizeResourceUrl(agent?.url, window.location.href)
        : '';
    if (url) window.open(url, '_blank', 'noopener');
}

// 渲染天气模板中的内联 Markdown，只开放安全的文本级样式，避免把 AIP 返回当成任意 HTML 注入。
function renderInlineMarkdown(text) {
    if (!text) return '';

    let html = escapeHTML(String(text));
    html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label, href) => {
        const safeHref = getSafeResourceUrl(href);
        if (!safeHref) return label;
        return `<a href="${escapeHTMLAttribute(safeHref)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    return html.replace(/\r?\n/g, '<br>');
}

// 设置智能体勾选框监听器
function setupAgentCheckboxListeners(root = document) {
    const panelElements = root?.classList?.contains('inline-agent-discovery')
        ? [root]
        : Array.from(root?.querySelectorAll?.('.inline-agent-discovery') || []);

    panelElements.forEach(panelElement => {
        if (panelElement.dataset.discoveryListenersBound === 'true') return;
        panelElement.dataset.discoveryListenersBound = 'true';
        const panel = getAgentDiscoveryPanel(panelElement.dataset.discoveryId);
        if (!panel) return;

        panelElement.querySelectorAll('.agent-checkbox').forEach(checkbox => {
            checkbox.addEventListener('change', event => {
                const agentName = event.target.value;
                if (event.target.checked) panel.selectedIoaAgents.add(agentName);
                else panel.selectedIoaAgents.delete(agentName);
                updateExecuteButton(panel.id);
            });
        });
        panelElement.querySelectorAll('.agent-assigned-task-input').forEach(editor => {
            editor.addEventListener('input', event => {
                const agent = panel.ioaAgents.find(item => item.name === event.target.dataset.agentName);
                if (agent) agent.assigned_task = event.target.value;
            });
        });
    });
}

// 创建消息 HTML
function createMessageHTML(msg) {
    if (msg.role === 'user') {
        const imageUrl = msg.image_url || msg.imageUrl;
        const imageHtml = imageUrl ? `<div class="message-image"><img src="${escapeHTMLAttribute(imageUrl)}" alt="图片"></div>` : '';
        return `
            <div class="message user">
                <div class="message-content-wrapper">
                    ${imageHtml}
                    <div class="message-content">${escapeHTML(msg.content)}</div>
                    ${renderCopyButton(msg.content)}
                    ${renderMessageMeta(msg)}
                </div>
            </div>
        `;
    } else {
        return `
            <div class="message assistant">
                <div class="message-avatar">
                    <img src="dingdang.png" alt="叮当">
                </div>
                <div class="message-content-wrapper">
                    <div class="message-content">${renderRichMessage(msg.content)}</div>
                    ${renderCopyButton(msg.content)}
                    ${renderMessageMeta(msg)}
                </div>
            </div>
        `;
    }
}

// 渲染消息元数据（时间统计）
function renderMessageMeta(msg) {
    const metadata = msg.message_metadata || {};
    const llmTime = metadata.llm_time;
    const dsTime = metadata.ds_time;
    
    if (llmTime !== undefined && llmTime !== null) {
        let metaHtml = `<div class="message-meta">`;
        
        if (llmTime > 0) {
            metaHtml += `<span class="meta-item">LLM: ${llmTime}s</span>`;
        }
        
        if (dsTime !== undefined && dsTime !== null && dsTime > 0) {
            metaHtml += `<span class="meta-item">DS: ${dsTime}s</span>`;
        }
        
        metaHtml += `</div>`;
        return metaHtml;
    }
    
    return '';
}

// 对 Markdown 行内语法做受控转换，供普通段落、列表和表格单元格共用。
function getSafeMarkdownLinkHref(href) {
    const value = String(href || '')
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .trim();
    if (!value || /[\u0000-\u001f\u007f"'<>]/.test(value)) return '';

    try {
        const parsed = new URL(value, window.location.href);
        if (!['http:', 'https:'].includes(parsed.protocol)) return '';
        if (parsed.username || parsed.password) return '';
        if (/^https?:/i.test(value)) return value;
        if (value.startsWith('//')) return parsed.href;
        return value;
    } catch {
        return '';
    }
}

function escapeHTMLAttribute(value) {
    return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function renderMarkdownInline(text) {
    let processed = String(text || '');
    processed = processed.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    processed = processed.replace(/`([^`]+)`/g, '<code>$1</code>');
    processed = processed.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, href) => {
        const safeHref = getSafeMarkdownLinkHref(href);
        if (!safeHref) return label;
        return `<a href="${escapeHTMLAttribute(safeHref)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });
    return processed;
}

// 拆分 Markdown pipe table 行；当前只处理常见表格，不尝试解析代码里的转义管道。
function splitMarkdownTableRow(line) {
    const trimmed = String(line || '').trim();
    if (!trimmed.includes('|')) return [];
    return trimmed
        .replace(/^\|/, '')
        .replace(/\|$/, '')
        .split('|')
        .map(cell => cell.trim());
}

// 查找下一条非空 Markdown 行，让 Wit 输出的“表格行之间空一行”也能被识别。
function findNextNonEmptyMarkdownLine(lines, startIndex) {
    for (let i = startIndex; i < lines.length; i++) {
        if (String(lines[i] || '').trim() !== '') return i;
    }
    return -1;
}

// 判断第二行是否是 Markdown 表格分隔行，并支持左右/居中对齐语法。
function isMarkdownTableSeparator(line) {
    const cells = splitMarkdownTableRow(line);
    if (cells.length < 2) return false;
    return cells.every(cell => /^:?-{3,}:?$/.test(cell.trim()));
}

// 从分隔行提取每一列的对齐方式，后续直接作用到 th/td。
function getMarkdownTableAlignments(separatorLine) {
    return splitMarkdownTableRow(separatorLine).map(cell => {
        const value = cell.trim();
        if (value.startsWith(':') && value.endsWith(':')) return 'center';
        if (value.endsWith(':')) return 'right';
        return 'left';
    });
}

// 补齐或裁剪单元格数量，避免 Wit 返回短行时破坏表格 DOM 结构。
function normalizeMarkdownTableCells(cells, columnCount) {
    const normalized = Array.from(cells || []);
    while (normalized.length < columnCount) normalized.push('');
    return normalized.slice(0, columnCount);
}

// 判断当前行是否可以继续作为表格数据行。
function isMarkdownTableDataRow(line, columnCount) {
    const cells = splitMarkdownTableRow(line);
    return cells.length >= 2 && cells.length <= Math.max(columnCount + 4, columnCount) && !isMarkdownTableSeparator(line);
}

// 将 pipe table 渲染成真正的 HTML 表格，移动端外层容器负责横向滚动。
function renderMarkdownTable(headerLine, separatorLine, bodyLines) {
    const alignments = getMarkdownTableAlignments(separatorLine);
    const columnCount = Math.max(splitMarkdownTableRow(headerLine).length, alignments.length);
    const headerCells = normalizeMarkdownTableCells(splitMarkdownTableRow(headerLine), columnCount);
    const rows = bodyLines.map(line => normalizeMarkdownTableCells(splitMarkdownTableRow(line), columnCount));
    const alignAttr = index => ` style="text-align: ${alignments[index] || 'left'}"`;
    const headerHtml = headerCells
        .map((cell, index) => `<th${alignAttr(index)}>${renderMarkdownInline(cell)}</th>`)
        .join('');
    const bodyHtml = rows
        .map(row => `<tr>${row.map((cell, index) => `<td${alignAttr(index)}>${renderMarkdownInline(cell)}</td>`).join('')}</tr>`)
        .join('');

    return `
        <div class="markdown-table-wrap">
            <table class="markdown-table">
                <thead><tr>${headerHtml}</tr></thead>
                <tbody>${bodyHtml}</tbody>
            </table>
        </div>
    `;
}

// Markdown 渲染
function renderMarkdown(text) {
    if (!text) return '';

    let html = text;

    // 围栏代码块提取（含 json-chart）：在 HTML 转义前提取，避免 JSON 中的 &<> 被二次转义
    const codeBlocks = [];
    html = html.replace(/```([\w-]*)\r?\n?([\s\S]*?)```/g, (m, lang, body) => {
        let rendered;
        if (lang === 'json-chart') {
            rendered = renderJsonChart(body);
        } else {
            rendered = '<pre class="code-block"><code>' + escapeHTML(body) + '</code></pre>';
        }
        const idx = codeBlocks.length;
        codeBlocks.push(rendered);
        return '\u0000BLOCK' + idx + '\u0000';
    });

    html = html.replace(/&/g, '&amp;');
    html = html.replace(/</g, '&lt;');
    html = html.replace(/>/g, '&gt;');

    // Support the complete CommonMark heading range.  In particular, agent
    // answers frequently use `####` for secondary sections.
    html = html.replace(/^(#{1,6})\s+(.+)$/gm, (match, marks, content) => {
        const level = marks.length;
        return `<h${level}>${content}</h${level}>`;
    });

    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    const lines = html.split('\n');
    const processedLines = [];
    let inList = false;
    let listBuffer = [];
    const flushList = () => {
        if (!inList) return;
        processedLines.push('<ul>' + listBuffer.join('') + '</ul>');
        inList = false;
        listBuffer = [];
    };

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const separatorLineIndex = findNextNonEmptyMarkdownLine(lines, i + 1);
        const nextTableLine = separatorLineIndex >= 0 ? lines[separatorLineIndex] : '';
        const tableHeaderCells = splitMarkdownTableRow(line);
        const listItemMatch = line.match(/^[\-\*] (.+)$/);

        if (tableHeaderCells.length >= 2 && isMarkdownTableSeparator(nextTableLine)) {
            flushList();
            const tableBodyLines = [];
            let rowIndex = separatorLineIndex + 1;
            while (rowIndex < lines.length) {
                if (String(lines[rowIndex] || '').trim() === '') {
                    const nextRowIndex = findNextNonEmptyMarkdownLine(lines, rowIndex + 1);
                    if (nextRowIndex >= 0 && isMarkdownTableDataRow(lines[nextRowIndex], tableHeaderCells.length)) {
                        rowIndex = nextRowIndex;
                        continue;
                    }
                    break;
                }
                if (!isMarkdownTableDataRow(lines[rowIndex], tableHeaderCells.length)) break;
                tableBodyLines.push(lines[rowIndex]);
                rowIndex += 1;
            }
            i = rowIndex - 1;
            processedLines.push(renderMarkdownTable(line, nextTableLine, tableBodyLines));
            continue;
        }

        if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
            flushList();
            processedLines.push('<hr>');
            continue;
        }

        if (listItemMatch) {
            if (!inList) {
                inList = true;
                listBuffer = [];
            }
            listBuffer.push('<li>' + renderMarkdownInline(listItemMatch[1]) + '</li>');
        } else {
            flushList();

            let processedLine = line;
            processedLine = renderMarkdownInline(processedLine);

            if (processedLine.trim() !== '') {
                // 占位符行不包裹 <p>，避免 block 元素嵌套进 <p>
                if (/\u0000BLOCK\d+\u0000/.test(processedLine) || /^<h[1-6]>[\s\S]*<\/h[1-6]>$/.test(processedLine)) {
                    processedLines.push(processedLine);
                } else {
                    processedLines.push('<p>' + processedLine + '</p>');
                }
            } else {
                processedLines.push('<br>');
            }
        }
    }

    if (inList) {
        processedLines.push('<ul>' + listBuffer.join('') + '</ul>');
    }

    html = processedLines.join('\n');

    // 回填围栏代码块占位符
    html = html.replace(/\u0000BLOCK(\d+)\u0000/g, (m, i) => codeBlocks[parseInt(i, 10)] || '');

    return html;
}

function registerMessageResource(resource) {
    const id = `message-resource-${++state.messageResourceSequence}`;
    state.messageResources[id] = { ...resource };
    return id;
}

function getInlineImageUrl(resource) {
    if (resource.url) return resource.url;
    const allowedMimeTypes = [
        'image/png',
        'image/jpeg',
        'image/gif',
        'image/webp',
        'image/avif'
    ];
    if (resource.bytes && allowedMimeTypes.includes(resource.mimeType)) {
        return `data:${resource.mimeType};base64,${resource.bytes}`;
    }
    return '';
}

function renderFileTypeIcon(category) {
    const labels = {
        pdf: 'PDF',
        document: 'DOC',
        spreadsheet: 'XLS',
        presentation: 'PPT',
        archive: 'ZIP',
        audio: 'AUD',
        video: 'VID',
        code: 'CODE',
        generic: 'FILE'
    };
    const safeCategory = escapeHTMLAttribute(category || 'generic');
    return `
        <span class="message-resource-file-icon message-resource-file-icon-${safeCategory}" aria-hidden="true">
            <svg viewBox="0 0 24 24" focusable="false">
                <path d="M6 2h8l4 4v16H6z"></path>
                <path d="M14 2v5h5"></path>
            </svg>
            <small>${labels[category] || 'FILE'}</small>
        </span>
    `;
}

function renderMessageResource(resource, options = {}) {
    const resourceId = options.resourceId || registerMessageResource(resource);
    const title = MESSAGE_RESOURCES.getResourceTitle
        ? MESSAGE_RESOURCES.getResourceTitle(resource)
        : '智能体返回的文件';
    const previewUrl = options.forceFile ? '' : getInlineImageUrl(resource);

    if (previewUrl && (resource.kind === 'image' || resource.kind === 'auto')) {
        const alt = resource.alt || title || '智能体返回的图片';
        return `
            <figure class="message-resource message-resource-image" data-message-resource-id="${escapeHTMLAttribute(resourceId)}">
                <button type="button" class="message-resource-image-button" data-message-image-open aria-label="${escapeHTMLAttribute(`全屏查看 ${alt}`)}">
                    <img src="${escapeHTMLAttribute(previewUrl)}" alt="${escapeHTMLAttribute(alt)}" loading="lazy" decoding="async">
                </button>
                ${title ? `<figcaption>${escapeHTML(title)}</figcaption>` : ''}
            </figure>
        `;
    }

    const category = MESSAGE_RESOURCES.getFileCategory
        ? MESSAGE_RESOURCES.getFileCategory(resource)
        : 'generic';
    const size = MESSAGE_RESOURCES.formatFileSize
        ? MESSAGE_RESOURCES.formatFileSize(resource.size)
        : '';
    const categoryLabels = {
        pdf: 'PDF',
        document: '文档',
        spreadsheet: '表格',
        presentation: '演示文稿',
        archive: '压缩包',
        audio: '音频',
        video: '视频',
        code: '代码',
        generic: resource.mimeType || '文件'
    };
    const meta = [categoryLabels[category] || '文件', size].filter(Boolean).join(' · ');
    return `
        <button type="button" class="message-resource message-resource-file" data-message-resource-id="${escapeHTMLAttribute(resourceId)}" data-message-download aria-label="${escapeHTMLAttribute(`下载 ${title}`)}">
            ${renderFileTypeIcon(category)}
            <span class="message-resource-file-copy">
                <strong title="${escapeHTMLAttribute(title)}">${escapeHTML(title)}</strong>
                <small>${escapeHTML(meta)}</small>
            </span>
            <span class="message-resource-download-icon" aria-hidden="true">↓</span>
        </button>
    `;
}

function renderRichMessage(content, options = {}) {
    if (!MESSAGE_RESOURCES.parseMessageResources) return renderMarkdown(content);
    const parsed = MESSAGE_RESOURCES.parseMessageResources(
        content,
        options.dataItems || [],
        window.location.href
    );
    const html = [];
    let resourceCards = [];

    function flushResources() {
        if (resourceCards.length === 0) return;
        html.push(`<div class="message-resource-list">${resourceCards.join('')}</div>`);
        resourceCards = [];
    }

    for (const segment of parsed.segments) {
        if (segment.type === 'resource') {
            resourceCards.push(renderMessageResource(segment.resource));
            continue;
        }
        if (!segment.content.trim() && resourceCards.length > 0) continue;
        flushResources();
        html.push(renderMarkdown(segment.content));
    }
    flushResources();
    return html.join('');
}

function openMessageImageLightbox(resourceId, trigger) {
    const resource = state.messageResources[resourceId];
    const src = resource && getInlineImageUrl(resource);
    if (!resource || !src || !elements.messageImageLightbox) return;

    state.messageImageLightboxTrigger = trigger || null;
    const title = MESSAGE_RESOURCES.getResourceTitle
        ? MESSAGE_RESOURCES.getResourceTitle(resource)
        : (resource.alt || '智能体返回的图片');
    elements.messageImageLightboxImage.src = src;
    elements.messageImageLightboxImage.alt = resource.alt || title;
    elements.messageImageLightboxTitle.textContent = title;
    elements.messageImageLightbox.hidden = false;
    document.body.classList.add('message-image-lightbox-open');
    elements.messageImageLightboxClose?.focus();
}

function closeMessageImageLightbox() {
    if (!elements.messageImageLightbox || elements.messageImageLightbox.hidden) return;

    elements.messageImageLightbox.hidden = true;
    elements.messageImageLightboxImage?.removeAttribute('src');
    document.body.classList.remove('message-image-lightbox-open');
    const trigger = state.messageImageLightboxTrigger;
    state.messageImageLightboxTrigger = null;
    if (trigger && document.contains(trigger)) trigger.focus();
}

function handleMessageResourceImageError(event) {
    const image = event.target.closest?.('.message-resource-image img');
    if (!image) return;
    const wrapper = image.closest('[data-message-resource-id]');
    const resourceId = wrapper?.dataset.messageResourceId;
    const resource = resourceId && state.messageResources[resourceId];
    if (!wrapper || !resource) return;

    wrapper.outerHTML = renderMessageResource(
        { ...resource, kind: 'file' },
        { resourceId, forceFile: true }
    );
}

function announceMessageResource(message) {
    if (elements.messageResourceStatus) {
        elements.messageResourceStatus.textContent = '';
        window.requestAnimationFrame(() => {
            elements.messageResourceStatus.textContent = message;
        });
    }
}

function base64ToBlob(bytes, mimeType) {
    const binary = atob(bytes);
    const chunks = [];
    for (let offset = 0; offset < binary.length; offset += 8192) {
        const slice = binary.slice(offset, offset + 8192);
        chunks.push(Uint8Array.from(slice, character => character.charCodeAt(0)));
    }
    return new Blob(chunks, { type: mimeType || 'application/octet-stream' });
}

function triggerBlobDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function downloadMessageResource(resourceId) {
    const resource = state.messageResources[resourceId];
    if (!resource) return;

    const filename = MESSAGE_RESOURCES.getResourceTitle
        ? MESSAGE_RESOURCES.getResourceTitle(resource)
        : (resource.name || '智能体返回的文件');
    try {
        if (resource.bytes) {
            triggerBlobDownload(base64ToBlob(resource.bytes, resource.mimeType), filename);
        } else if (resource.url) {
            const parsed = new URL(resource.url, window.location.href);
            const response = await fetch(parsed.href, {
                credentials: parsed.origin === window.location.origin ? 'same-origin' : 'omit'
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            triggerBlobDownload(await response.blob(), filename);
        } else {
            throw new Error('资源没有可下载内容');
        }
        announceMessageResource(`${filename} 已开始下载`);
    } catch (error) {
        console.warn('[Dingdang] 下载消息资源失败:', error);
        if (resource.url) {
            window.open(resource.url, '_blank', 'noopener,noreferrer');
            announceMessageResource(`无法直接下载 ${filename}，已打开原始链接`);
        } else {
            announceMessageResource(`${filename} 下载失败`);
        }
    }
}

function getAgentResultDataItems(event) {
    return MESSAGE_RESOURCES.collectEventDataItems
        ? MESSAGE_RESOURCES.collectEventDataItems(event)
        : [];
}

function renderAgentDataItemResources(event, content = '') {
    const items = getAgentResultDataItems(event);
    return renderUnreferencedDataItemResources(content, items);
}

function renderUnreferencedDataItemResources(content, dataItems) {
    if (!Array.isArray(dataItems)
        || dataItems.length === 0
        || !MESSAGE_RESOURCES.parseMessageResources) return '';

    const parsed = MESSAGE_RESOURCES.parseMessageResources(
        content,
        dataItems,
        window.location.href
    );
    const textOnly = MESSAGE_RESOURCES.parseMessageResources(content, [], window.location.href);
    const textKeys = new Set(textOnly.resources.map(MESSAGE_RESOURCES.resourceKey));
    const cards = parsed.resources
        .filter(resource => !textKeys.has(MESSAGE_RESOURCES.resourceKey(resource)))
        .map(resource => renderMessageResource(resource));
    return cards.length
        ? `<div class="message-resource-list">${cards.join('')}</div>`
        : '';
}

// 最终 SSE 事件携带的 JSON 摘要仅由后端生成；前端按范围替换原 JSON，并保留本地可展开原文。
function renderChatJsonSummaries(content, summaries, options = {}) {
    const dataItems = options.dataItems || [];
    if (!Array.isArray(summaries) || summaries.length === 0) {
        return renderRichMessage(content, { dataItems });
    }
    let cursor = 0;
    const parts = [];
    summaries.forEach((summary, index) => {
        const start = Number(summary?.start);
        const end = Number(summary?.end);
        if (!Number.isInteger(start) || !Number.isInteger(end) || start < cursor || end < start || end > content.length) return;
        parts.push(renderRichMessage(content.slice(cursor, start)));
        const rawId = `chat-json-summary-${Date.now()}-${index}`;
        parts.push(`<section class="chat-json-summary"><div class="chat-json-summary-label">根据 JSON 整理</div><div class="chat-json-summary-markdown">${renderRichMessage(summary.markdown || '')}</div><button type="button" class="chat-json-summary-toggle" ${renderDynamicActionAttributes('toggle-json-summary', { rawId })}>查看原始 JSON</button><pre id="${rawId}" class="chat-json-summary-raw" hidden><code>${escapeHTML(summary.raw_json || '')}</code></pre></section>`);
        cursor = end;
    });
    parts.push(renderRichMessage(content.slice(cursor)));
    parts.push(renderUnreferencedDataItemResources(content, dataItems));
    return parts.join('');
}

function toggleChatJsonSummary(rawId, button) {
    const raw = document.getElementById(rawId);
    if (!raw) return;
    raw.hidden = !raw.hidden;
    button.textContent = raw.hidden ? '查看原始 JSON' : '收起原始 JSON';
}

// HTML 转义
function escapeHTML(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============ json-chart 图表渲染（纯 SVG，无外部依赖）============

// 调色板（亮色，适配深色玻璃拟态背景）
const CHART_PALETTE = ['#60a5fa', '#f472b6', '#34d399', '#fbbf24', '#a78bfa', '#f87171', '#22d3ee', '#fb923c'];

// 数值格式化（最多 2 位小数，去除尾部 0）
function formatChartNum(v) {
    if (typeof v !== 'number') v = parseFloat(v);
    if (isNaN(v)) return '0';
    if (Number.isInteger(v)) return String(v);
    return parseFloat(v.toFixed(2)).toString();
}

// 文本截断
function truncateText(s, n) {
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

// 柱状图
function renderBar(data) {
    const categories = data.categories || [];
    const series = data.series || [];
    const N = categories.length;
    const M = series.length;
    if (N === 0 || M === 0) {
        return '<div class="chart-block chart-error"><div class="chart-error-title">📊 图表数据为空</div></div>';
    }
    let maxVal = 0;
    series.forEach(s => { (s.data || []).forEach(v => { if (v > maxVal) maxVal = v; }); });
    if (maxVal <= 0) maxVal = 1;

    const W = 320;
    const padL = 34, padR = 10, padT = 10;
    const plotW = W - padL - padR;
    const plotH = 150;
    const xLabelH = 40, legendH = 22;
    const H = padT + plotH + xLabelH + legendH + 10;
    const plotBottom = padT + plotH;
    const groupW = plotW / N;
    const barW = Math.max(2, (groupW * 0.7) / M);
    const groupGap = groupW * 0.15;

    // 网格线
    let grid = '';
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
        const y = plotBottom - (plotH * i / ticks);
        const val = maxVal * i / ticks;
        grid += '<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (W - padR) + '" y2="' + y.toFixed(1) + '" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>';
        grid += '<text x="' + (padL - 4) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end" font-size="9" fill="rgba(255,255,255,0.6)">' + escapeHTML(formatChartNum(val)) + '</text>';
    }

    // 柱子 + 数值标签
    let bars = '';
    series.forEach((s, si) => {
        const color = CHART_PALETTE[si % CHART_PALETTE.length];
        (s.data || []).forEach((v, ci) => {
            const h = plotH * (v / maxVal);
            const x = padL + ci * groupW + groupGap + si * barW;
            const y = plotBottom - h;
            bars += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + h.toFixed(1) + '" fill="' + color + '" rx="2"/>';
            bars += '<text x="' + (x + barW / 2).toFixed(1) + '" y="' + (y - 3).toFixed(1) + '" text-anchor="middle" font-size="9" fill="rgba(255,255,255,0.85)">' + escapeHTML(formatChartNum(v)) + '</text>';
        });
    });

    // x 轴标签（旋转 -20°，避免重叠）
    let xLabels = '';
    categories.forEach((c, ci) => {
        const cx = padL + ci * groupW + groupW / 2;
        const ly = plotBottom + 14;
        xLabels += '<text x="' + cx.toFixed(1) + '" y="' + ly + '" text-anchor="end" font-size="9" fill="rgba(255,255,255,0.85)" transform="rotate(-20 ' + cx.toFixed(1) + ' ' + ly + ')">' + escapeHTML(truncateText(c, 12)) + '</text>';
    });

    // 图例
    let legend = '';
    series.forEach((s, si) => {
        const color = CHART_PALETTE[si % CHART_PALETTE.length];
        const lx = padL + si * 80;
        const ly = H - 8;
        legend += '<rect x="' + lx + '" y="' + (ly - 9) + '" width="10" height="10" fill="' + color + '" rx="2"/>';
        legend += '<text x="' + (lx + 14) + '" y="' + ly + '" font-size="9" fill="rgba(255,255,255,0.85)">' + escapeHTML(truncateText(s.name || '', 10)) + '</text>';
    });

    const titleText = getChartTitleText(data);
    const titleHtml = titleText ? '<div class="chart-title">' + escapeHTML(titleText) + '</div>' : '';
    return '<div class="chart-block">' + titleHtml + '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="height:auto" preserveAspectRatio="xMidYMid meet">' + grid + bars + xLabels + legend + '</svg></div>';
}

// 折线图
function renderLine(data) {
    const categories = data.categories || [];
    const series = data.series || [];
    const N = categories.length;
    const M = series.length;
    if (N === 0 || M === 0) {
        return '<div class="chart-block chart-error"><div class="chart-error-title">📊 图表数据为空</div></div>';
    }
    let maxVal = 0;
    series.forEach(s => { (s.data || []).forEach(v => { if (v > maxVal) maxVal = v; }); });
    if (maxVal <= 0) maxVal = 1;

    const W = 320;
    const padL = 34, padR = 10, padT = 10;
    const plotW = W - padL - padR;
    const plotH = 150;
    const xLabelH = 40, legendH = 22;
    const H = padT + plotH + xLabelH + legendH + 10;
    const plotBottom = padT + plotH;
    const stepX = N > 1 ? plotW / (N - 1) : 0;

    // 网格线
    let grid = '';
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
        const y = plotBottom - (plotH * i / ticks);
        const val = maxVal * i / ticks;
        grid += '<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (W - padR) + '" y2="' + y.toFixed(1) + '" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>';
        grid += '<text x="' + (padL - 4) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end" font-size="9" fill="rgba(255,255,255,0.6)">' + escapeHTML(formatChartNum(val)) + '</text>';
    }

    // 折线 + 数据点
    let lines = '';
    series.forEach((s, si) => {
        const color = CHART_PALETTE[si % CHART_PALETTE.length];
        const pts = (s.data || []).map((v, ci) => {
            const x = N > 1 ? padL + ci * stepX : padL + plotW / 2;
            const y = plotBottom - plotH * (v / maxVal);
            return [x, y];
        });
        const polyPts = pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
        lines += '<polyline points="' + polyPts + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
        pts.forEach(p => {
            lines += '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="2.5" fill="' + color + '"/>';
        });
    });

    // x 轴标签
    let xLabels = '';
    categories.forEach((c, ci) => {
        const cx = N > 1 ? padL + ci * stepX : padL + plotW / 2;
        const ly = plotBottom + 14;
        xLabels += '<text x="' + cx.toFixed(1) + '" y="' + ly + '" text-anchor="end" font-size="9" fill="rgba(255,255,255,0.85)" transform="rotate(-20 ' + cx.toFixed(1) + ' ' + ly + ')">' + escapeHTML(truncateText(c, 12)) + '</text>';
    });

    // 图例
    let legend = '';
    series.forEach((s, si) => {
        const color = CHART_PALETTE[si % CHART_PALETTE.length];
        const lx = padL + si * 80;
        const ly = H - 8;
        legend += '<rect x="' + lx + '" y="' + (ly - 9) + '" width="10" height="10" fill="' + color + '" rx="2"/>';
        legend += '<text x="' + (lx + 14) + '" y="' + ly + '" font-size="9" fill="rgba(255,255,255,0.85)">' + escapeHTML(truncateText(s.name || '', 10)) + '</text>';
    });

    const titleText = getChartTitleText(data);
    const titleHtml = titleText ? '<div class="chart-title">' + escapeHTML(titleText) + '</div>' : '';
    return '<div class="chart-block">' + titleHtml + '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="height:auto" preserveAspectRatio="xMidYMid meet">' + grid + lines + xLabels + legend + '</svg></div>';
}

// 饼图
function renderPie(data) {
    const categories = data.categories || [];
    const series = (data.series && data.series[0]) || { data: [] };
    const values = series.data || [];
    const N = Math.min(categories.length, values.length);
    if (N === 0) {
        return '<div class="chart-block chart-error"><div class="chart-error-title">📊 图表数据为空</div></div>';
    }
    const total = values.reduce((a, b) => a + (b > 0 ? b : 0), 0);
    if (total <= 0) {
        return '<div class="chart-block chart-error"><div class="chart-error-title">📊 图表数据总和为 0</div></div>';
    }

    const W = 320, H = 200;
    const cx = 85, cy = 100, r = 72;
    let startAngle = -Math.PI / 2; // 从顶部开始
    let slices = '';
    for (let i = 0; i < N; i++) {
        const v = values[i];
        if (v <= 0) continue;
        const angle = (v / total) * Math.PI * 2;
        const endAngle = startAngle + angle;
        const x1 = cx + r * Math.cos(startAngle);
        const y1 = cy + r * Math.sin(startAngle);
        const x2 = cx + r * Math.cos(endAngle);
        const y2 = cy + r * Math.sin(endAngle);
        const largeArc = angle > Math.PI ? 1 : 0;
        const color = CHART_PALETTE[i % CHART_PALETTE.length];
        const d = 'M ' + cx + ' ' + cy + ' L ' + x1.toFixed(2) + ' ' + y1.toFixed(2) + ' A ' + r + ' ' + r + ' 0 ' + largeArc + ' 1 ' + x2.toFixed(2) + ' ' + y2.toFixed(2) + ' Z';
        slices += '<path d="' + d + '" fill="' + color + '" stroke="rgba(0,0,0,0.25)" stroke-width="1"/>';
        startAngle = endAngle;
    }

    // 图例（右侧：category + 数值 + 百分比）
    let legend = '';
    for (let i = 0; i < N; i++) {
        const v = values[i];
        const pct = (v / total) * 100;
        const color = CHART_PALETTE[i % CHART_PALETTE.length];
        const ly = 24 + i * 20;
        legend += '<rect x="175" y="' + (ly - 9) + '" width="10" height="10" fill="' + color + '" rx="2"/>';
        legend += '<text x="191" y="' + ly + '" font-size="9" fill="rgba(255,255,255,0.85)">' + escapeHTML(truncateText(categories[i] || '', 14)) + ' (' + formatChartNum(v) + ', ' + pct.toFixed(1) + '%)</text>';
    }

    const titleText = getChartTitleText(data);
    const titleHtml = titleText ? '<div class="chart-title">' + escapeHTML(titleText) + '</div>' : '';
    return '<div class="chart-block">' + titleHtml + '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="height:auto" preserveAspectRatio="xMidYMid meet">' + slices + legend + '</svg></div>';
}

// 表格
function renderTable(data) {
    const categories = data.categories || [];
    const series = data.series || [];
    let html = '<table class="chart-table"><thead><tr><th></th>';
    categories.forEach(c => {
        html += '<th>' + escapeHTML(String(c)) + '</th>';
    });
    html += '</tr></thead><tbody>';
    series.forEach(s => {
        html += '<tr><td class="chart-table-row-label">' + escapeHTML(String(s.name || '')) + '</td>';
        (s.data || []).forEach(v => {
            html += '<td>' + escapeHTML(formatChartNum(v)) + '</td>';
        });
        html += '</tr>';
    });
    html += '</tbody></table>';
    const titleText = getChartTitleText(data);
    const titleHtml = titleText ? '<div class="chart-title">' + escapeHTML(titleText) + '</div>' : '';
    return '<div class="chart-block">' + titleHtml + html + '</div>';
}

// json-chart 渲染入口：解析失败/未知类型/渲染异常一律降级，绝不抛错阻断消息渲染
function renderJsonChart(jsonStr) {
    let data;
    try {
        data = JSON.parse(jsonStr);
    } catch (e) {
        return '<div class="chart-block chart-error"><div class="chart-error-title">📊 图表数据解析失败</div><pre class="chart-raw">' + escapeHTML(jsonStr) + '</pre></div>';
    }
    try {
        switch (data.type) {
            case 'bar': return renderBar(data);
            case 'line': return renderLine(data);
            case 'pie': return renderPie(data);
            case 'table': return renderAgentChart(data);
            default:
                if (normalizeAgentChartTable(data).rows.length > 0) return renderAgentChart(data);
                return '<div class="chart-block chart-error"><div class="chart-error-title">📊 暂不支持的图表类型: ' + escapeHTML(data.type || '未知') + '</div><pre class="chart-raw">' + escapeHTML(JSON.stringify(data, null, 2)) + '</pre></div>';
        }
    } catch (e) {
        try {
            return '<div class="chart-block chart-error"><div class="chart-error-title">📊 图表渲染异常</div><pre class="chart-raw">' + escapeHTML(JSON.stringify(data, null, 2)) + '</pre></div>';
        } catch (e2) {
            return '<div class="chart-block chart-error"><div class="chart-error-title">📊 图表渲染异常</div></div>';
        }
    }
}

// 读取 AIP DataItem 文本，兼容 TextDataItem 的 text 字段和少量历史 data 字段。
function getDataItemText(item) {
    if (!item || typeof item !== 'object') return '';
    if (typeof item.text === 'string' && item.text.trim()) return item.text;
    if (typeof item.data === 'string' && item.data.trim()) return item.data;
    if (item.data && typeof item.data.text === 'string' && item.data.text.trim()) return item.data.text;
    return '';
}

// 从 agent_result_card 事件中提取最终展示内容，优先使用 Product.dataItems。
function extractAgentCardContent(event) {
    const productTexts = [];
    const products = Array.isArray(event.products) ? event.products : [];

    products.forEach(product => {
        const items = Array.isArray(product?.dataItems)
            ? product.dataItems
            : (Array.isArray(product?.data_items) ? product.data_items : []);
        items.forEach(item => {
            const text = getDataItemText(item);
            if (text) productTexts.push(text);
        });
    });

    if (productTexts.length > 0) {
        return productTexts.join('\n\n');
    }

    if (typeof event.content === 'string' && event.content.trim()) {
        return event.content;
    }

    const statusTexts = [];
    const statusItems = Array.isArray(event.statusDataItems) ? event.statusDataItems : [];
    statusItems.forEach(item => {
        const text = getDataItemText(item);
        if (text) statusTexts.push(text);
    });
    return statusTexts.join('\n\n');
}

// 将 Markdown 拆成普通文本块和 json-chart 图表块，避免图表 JSON 被普通 Markdown 渲染破坏。
function parseAgentCardParts(markdown) {
    const parts = [];
    const source = markdown || '';
    const fenceRegex = /```([a-zA-Z0-9_-]*)\s*\n([\s\S]*?)```/g;
    let cursor = 0;
    let match;

    while ((match = fenceRegex.exec(source)) !== null) {
        if (match.index > cursor) {
            parts.push({ type: 'markdown', content: source.slice(cursor, match.index) });
        }

        const lang = (match[1] || '').trim().toLowerCase();
        const content = match[2] || '';
        if (lang === 'json-chart') {
            parts.push({ type: 'chart', content });
        } else {
            parts.push({ type: 'markdown', content: `\`\`\`${lang}\n${content}\`\`\`` });
        }
        cursor = fenceRegex.lastIndex;
    }

    if (cursor < source.length) {
        parts.push({ type: 'markdown', content: source.slice(cursor) });
    }

    return parts.length > 0 ? parts : [{ type: 'markdown', content: source }];
}

// 清理 Wit prompt 中可能出现的 JSONC 注释和尾逗号，尽量兼容演示输出。
function stripJsonComments(jsonText) {
    return (jsonText || '')
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/(^|[^:])\/\/.*$/gm, '$1')
        .replace(/,\s*([}\]])/g, '$1')
        .trim();
}

// 解析 json-chart 块，非法 JSON 返回 null，由调用方回落为代码块展示。
function parseJsonChart(chartText) {
    try {
        return JSON.parse(stripJsonComments(chartText));
    } catch (error) {
        console.warn('Invalid json-chart block:', error);
        return null;
    }
}

// 将任意图表序列转换为数值数组，无法转换的值按 0 处理，保证移动端渲染稳定。
function normalizeChartValues(series) {
    return (Array.isArray(series) ? series : []).map(value => {
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }).filter(value => value !== null);
}

// 将 json-chart 的标题归一成字符串，避免对象标题在界面中显示成 [object Object]。
function getChartTitleText(chart) {
    const title = chart?.title;
    if (title === undefined || title === null || title === '') return '';
    if (typeof title === 'string' || typeof title === 'number' || typeof title === 'boolean') {
        return String(title);
    }
    if (typeof title === 'object') {
        for (const key of ['text', 'title', 'name', 'label', 'value', 'content']) {
            const value = title[key];
            if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
                return String(value);
            }
        }
    }
    return '数据表';
}

// 判断一个值是否是普通对象，表格归一化时用它区分对象行和数组行。
function isPlainObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// 将任意表格列声明转换为用户可读的列名。
function normalizeChartColumnLabel(column, index) {
    if (typeof column === 'string' || typeof column === 'number') return String(column);
    if (isPlainObject(column)) {
        return String(column.label || column.title || column.name || column.header || column.key || column.field || `列 ${index + 1}`);
    }
    return `列 ${index + 1}`;
}

// 将任意表格列声明转换为读取对象行时使用的字段名。
function normalizeChartColumnKey(column, index) {
    if (typeof column === 'string' || typeof column === 'number') return String(column);
    if (isPlainObject(column)) {
        return String(column.key || column.field || column.dataIndex || column.name || column.label || column.title || `col_${index + 1}`);
    }
    return `col_${index + 1}`;
}

// 将表格单元格转换成适合移动端展示的短文本，保留对象里的 value/text 等常见字段。
function normalizeChartCellValue(value) {
    if (value === undefined || value === null) return '';
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (Array.isArray(value)) return value.map(normalizeChartCellValue).filter(Boolean).join(' / ');
    if (isPlainObject(value)) {
        for (const key of ['text', 'value', 'label', 'name', 'title', 'content']) {
            if (value[key] !== undefined && value[key] !== null) return normalizeChartCellValue(value[key]);
        }
        try {
            return JSON.stringify(value);
        } catch {
            return String(value);
        }
    }
    return String(value);
}

// 从对象行里推断列集合，兼容 Wit 把 json-chart 输出为 rows/data 对象数组的情况。
function inferChartColumnsFromObjectRows(rows) {
    const keys = [];
    (Array.isArray(rows) ? rows : []).forEach(row => {
        if (!isPlainObject(row)) return;
        Object.keys(row).forEach(key => {
            if (!keys.includes(key)) keys.push(key);
        });
    });
    return keys;
}

// 将 json-chart 的多种表格形态归一为 columns + rows，保证表格型返回不会误进空图表。
function normalizeAgentChartTable(chart) {
    if (!chart || typeof chart !== 'object') return { columns: [], rows: [] };

    const nestedTable = chart.table || chart.dataset || chart.dataTable;
    if (nestedTable && typeof nestedTable === 'object' && nestedTable !== chart) {
        const nested = normalizeAgentChartTable({ ...nestedTable, title: nestedTable.title || chart.title });
        if (nested.rows.length > 0) return nested;
    }

    const rowSource = Array.isArray(chart.rows)
        ? chart.rows
        : Array.isArray(chart.data)
            ? chart.data
            : Array.isArray(chart.values)
                ? chart.values
                : [];
    const declaredColumns = Array.isArray(chart.columns)
        ? chart.columns
        : Array.isArray(chart.headers)
            ? chart.headers
            : [];

    if (rowSource.length > 0 && rowSource.every(isPlainObject)) {
        const columns = declaredColumns.length > 0 ? declaredColumns : inferChartColumnsFromObjectRows(rowSource);
        const columnKeys = columns.map(normalizeChartColumnKey);
        return {
            columns: columns.map(normalizeChartColumnLabel),
            rows: rowSource.map(row => columnKeys.map(key => normalizeChartCellValue(row[key]))),
        };
    }

    if (rowSource.length > 0 && rowSource.every(Array.isArray)) {
        const widest = Math.max(...rowSource.map(row => row.length), declaredColumns.length, 0);
        const columns = declaredColumns.length > 0
            ? declaredColumns.map(normalizeChartColumnLabel)
            : Array.from({ length: widest }, (_, index) => `列 ${index + 1}`);
        return {
            columns,
            rows: rowSource.map(row => Array.from({ length: columns.length }, (_, index) => normalizeChartCellValue(row[index]))),
        };
    }

    const categories = Array.isArray(chart.categories) ? chart.categories : [];
    const series = Array.isArray(chart.series) ? chart.series : [];
    if (categories.length > 0 && series.length > 0) {
        return {
            columns: ['指标', ...categories.map(item => normalizeChartCellValue(item))],
            rows: series.map((item, index) => [
                normalizeChartCellValue(item?.name || `序列 ${index + 1}`),
                ...(Array.isArray(item?.data) ? item.data.map(normalizeChartCellValue) : [])
            ]),
        };
    }

    if (series.length > 0 && series.some(item => Array.isArray(item?.data) && item.data.some(isPlainObject))) {
        const rows = series.flatMap(item => Array.isArray(item?.data) ? item.data.filter(isPlainObject) : []);
        const columns = inferChartColumnsFromObjectRows(rows);
        return {
            columns,
            rows: rows.map(row => columns.map(key => normalizeChartCellValue(row[key]))),
        };
    }

    return { columns: [], rows: [] };
}

// 判断图表是否含有可绘制的数值序列；没有数值时优先回落成表格。
function hasDrawableChartSeries(chart) {
    const series = Array.isArray(chart?.series) ? chart.series : [];
    return series.some(item => normalizeChartValues(item?.data).length > 0);
}

// 渲染 Agent json-chart 表格，复用 Markdown 表格样式保持移动端横向滚动行为一致。
function renderAgentChartTable(chart) {
    const table = normalizeAgentChartTable(chart);
    if (!table.columns.length || !table.rows.length) {
        return '<div class="agent-chart-empty">暂无可展示表格数据</div>';
    }
    const headerHtml = table.columns
        .map(column => `<th>${escapeHTML(normalizeChartCellValue(column))}</th>`)
        .join('');
    const bodyHtml = table.rows
        .map(row => `<tr>${table.columns.map((_, index) => `<td>${escapeHTML(normalizeChartCellValue(row[index]))}</td>`).join('')}</tr>`)
        .join('');
    return `
        <div class="markdown-table-wrap agent-chart-table-wrap">
            <table class="markdown-table agent-chart-table">
                <thead><tr>${headerHtml}</tr></thead>
                <tbody>${bodyHtml}</tbody>
            </table>
        </div>
    `;
}

// 渲染 line 类型图表，使用内联 SVG 避免 P0 引入额外图表库。
function renderLineChart(chart) {
    const firstSeries = Array.isArray(chart.series) ? chart.series[0] : null;
    const values = normalizeChartValues(firstSeries?.data);
    if (values.length === 0) return '<div class="agent-chart-empty">暂无可绘制数据</div>';

    const width = 320;
    const height = 150;
    const padding = 18;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const step = values.length > 1 ? (width - padding * 2) / (values.length - 1) : 0;
    const points = values.map((value, index) => {
        const x = padding + index * step;
        const y = height - padding - ((value - min) / range) * (height - padding * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');

    return `
        <svg class="agent-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHTMLAttribute(getChartTitleText(chart) || '趋势图')}">
            <polyline class="agent-chart-line" points="${points}"></polyline>
            ${values.map((value, index) => {
                const [x, y] = points.split(' ')[index].split(',');
                return `<circle class="agent-chart-point" cx="${x}" cy="${y}" r="3"><title>${escapeHTML(String(value))}</title></circle>`;
            }).join('')}
        </svg>
    `;
}

// 渲染 bar 类型图表，适合天气、财经和游戏在线人数等少量分类数据。
function renderBarChart(chart) {
    const firstSeries = Array.isArray(chart.series) ? chart.series[0] : null;
    const values = normalizeChartValues(firstSeries?.data);
    const categories = Array.isArray(chart.categories) ? chart.categories : values.map((_, index) => `#${index + 1}`);
    const max = Math.max(...values, 1);

    if (values.length === 0) return '<div class="agent-chart-empty">暂无可绘制数据</div>';

    return `
        <div class="agent-chart-bars">
            ${values.map((value, index) => {
                const height = Math.max(6, Math.round((value / max) * 100));
                return `
                    <div class="agent-chart-bar-item">
                        <div class="agent-chart-bar-value">${escapeHTML(String(value))}</div>
                        <div class="agent-chart-bar" style="height:${height}%"></div>
                        <div class="agent-chart-label">${escapeHTML(String(categories[index] || ''))}</div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

// 渲染 pie 类型图表；P0 用图例化摘要表达占比，避免复杂扇区算法影响稳定性。
function renderPieChart(chart) {
    const firstSeries = Array.isArray(chart.series) ? chart.series[0] : null;
    const values = normalizeChartValues(firstSeries?.data);
    const categories = Array.isArray(chart.categories) ? chart.categories : values.map((_, index) => `项目 ${index + 1}`);
    const total = values.reduce((sum, value) => sum + value, 0) || 1;

    if (values.length === 0) return '<div class="agent-chart-empty">暂无可绘制数据</div>';

    return `
        <div class="agent-chart-pie-list">
            ${values.map((value, index) => {
                const percent = ((value / total) * 100).toFixed(1);
                return `
                    <div class="agent-chart-pie-row">
                        <span class="agent-chart-pie-dot"></span>
                        <span class="agent-chart-pie-name">${escapeHTML(String(categories[index] || ''))}</span>
                        <span class="agent-chart-pie-value">${escapeHTML(String(value))} · ${percent}%</span>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

// 按 Wit json-chart 约定渲染图表，未知类型回落为数据表。
function renderAgentChart(chart) {
    if (!chart) return '';
    const type = String(chart.type || 'bar').toLowerCase();
    const titleText = getChartTitleText(chart);
    const title = titleText ? `<div class="agent-chart-title">${escapeHTML(titleText)}</div>` : '';
    let body = '';

    if (type === 'table') {
        body = renderAgentChartTable(chart);
    } else if (!hasDrawableChartSeries(chart) && normalizeAgentChartTable(chart).rows.length > 0) {
        body = renderAgentChartTable(chart);
    } else if (type === 'line') {
        body = renderLineChart(chart);
    } else if (type === 'pie') {
        body = renderPieChart(chart);
    } else {
        body = renderBarChart(chart);
    }

    return `<div class="agent-chart">${title}${body}</div>`;
}

// 统一读取后端渲染提示，兼容 snake_case 与 camelCase 字段。
function getAgentRenderHints(event) {
    return event.render_hints || event.renderHints || {};
}

// 判断后端是否把当前结果归一为“等待输入”，用于 A2UI 卡片进入多轮交互态。
function isAgentAwaitingInput(event) {
    const hints = getAgentRenderHints(event);
    const stateName = String(event?.state || event?.status || '').toLowerCase();
    return stateName === 'awaiting-input'
        || stateName === 'awaiting_input'
        || stateName === 'waiting-input'
        || stateName === 'waiting_input'
        || hints.needs_input === true
        || hints.needsInput === true;
}

// 保存最新 Agent 卡片事件，天气应用中的输入控件会用它继续同一个 AIP 任务。
function rememberAgentResultEvent(event) {
    const ref = getAgentResultCardDomId(event);
    state.agentResultEvents[ref] = event;
    if (!isAgentAwaitingInput(event) && state.weatherAgentApp.continuingByCard[ref]?.status === 'pending') {
        delete state.weatherAgentApp.continuingByCard[ref];
    }
    return ref;
}

function setActiveAgentInteraction(event) {
    if (!event || !isAgentAwaitingInput(event)) return;
    state.activeAgentInteraction = event;
    const name = event.agent_name || event.agent || '智能体';
    if (elements.agentInteractionStatus) {
        elements.agentInteractionStatus.hidden = false;
        elements.agentInteractionStatus.classList.remove('is-hidden');
        elements.agentInteractionStatus.classList.add('is-flowing');
        elements.agentInteractionStatusTitle.textContent = `${name} 正在等待你的补充`;
        elements.agentInteractionStatusHint.textContent = '请根据智能体的提示，在下方输入框中继续回复。';
    }
    if (elements.agentInteractionEndBtn) elements.agentInteractionEndBtn.hidden = event.cancelable === false;
    if (elements.messageInput) {
        elements.messageInput.placeholder = `回复 ${name}…`;
        elements.messageInput.focus();
    }
    if (elements.imageUploadBtn) elements.imageUploadBtn.disabled = true;
}

function clearActiveAgentInteraction() {
    state.activeAgentInteraction = null;
    if (elements.agentInteractionStatus) {
        elements.agentInteractionStatus.classList.remove('is-flowing');
        elements.agentInteractionStatus.classList.add('is-hidden');
        elements.agentInteractionStatus.hidden = true;
    }
    if (elements.agentInteractionEndBtn) elements.agentInteractionEndBtn.hidden = false;
    if (elements.messageInput) elements.messageInput.placeholder = '输入你的消息...';
    if (elements.imageUploadBtn) elements.imageUploadBtn.disabled = false;
}

function restoreActiveAgentInteraction(interaction) {
    const interactionState = String(interaction.state || interaction.status || 'awaiting-input').toLowerCase();
    const restored = {
        ...interaction,
        conversation_id: state.currentConversationId,
        state: interactionState,
        status: interactionState,
        content: interaction.question || ''
    };
    if (restored.interaction_kind === 'collaboration') {
        const collaboration = interaction.collaboration || {};
        state.pendingSecretaryCollaboration = {
            userQuery: collaboration.userQuery || collaboration.user_query || '',
            selectedAgents: collaboration.selectedAgents || collaboration.selected_agents || [],
            ioaAgents: collaboration.ioaAgents || collaboration.ioa_agents || [],
            attachmentIds: collaboration.attachmentIds || collaboration.attachment_ids || [],
            question: restored.question || '',
            runId: restored.run_id || ''
        };
        restored.collaboration = state.pendingSecretaryCollaboration;
        if (restored.cancelable === undefined) restored.cancelable = Boolean(restored.run_id);
        restored.agent = restored.agent || '叮当';
        restored.agent_name = restored.agent_name || restored.agent;
    }
    setActiveAgentInteraction(restored);
}

function isSameAgentInteraction(activeEvent, nextEvent) {
    if (!activeEvent || !nextEvent) return false;
    const activeTaskId = String(activeEvent.task_id || activeEvent.taskId || '');
    const nextTaskId = String(nextEvent.task_id || nextEvent.taskId || '');
    if (activeTaskId && nextTaskId) return activeTaskId === nextTaskId;
    const activeAic = String(activeEvent.aic || '');
    const nextAic = String(nextEvent.aic || '');
    return Boolean(activeAic && nextAic && activeAic === nextAic
        && String(activeEvent.endpoint || '') === String(nextEvent.endpoint || ''));
}

// 读取浏览器定位；失败时返回状态而不是抛错，保证演示链路可继续到 awaiting-input。
function getBrowserLocationContext(timeoutMs = 6000) {
    if (!navigator.geolocation) {
        return Promise.resolve({ location_status: 'unsupported' });
    }

    return new Promise(resolve => {
        let settled = false;
        const finish = value => {
            if (settled) return;
            settled = true;
            resolve(value);
        };
        const timer = setTimeout(() => finish({ location_status: 'timeout' }), timeoutMs);

        navigator.geolocation.getCurrentPosition(
            position => {
                clearTimeout(timer);
                finish({
                    location_status: 'available',
                    location: {
                        latitude: position.coords.latitude,
                        longitude: position.coords.longitude,
                        accuracy: position.coords.accuracy,
                        timestamp: position.timestamp
                    }
                });
            },
            error => {
                clearTimeout(timer);
                const statusMap = {
                    1: 'denied',
                    2: 'unavailable',
                    3: 'timeout'
                };
                finish({ location_status: statusMap[error.code] || 'error' });
            },
            {
                enableHighAccuracy: false,
                timeout: timeoutMs,
                maximumAge: 300000
            }
        );
    });
}

// 构造传给叮当后端的客户端上下文；天气任务会额外尝试附带当前位置。
async function buildClientContext(options = {}) {
    const context = {
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
        locale: navigator.language || ''
    };

    if (options.requireLocation) {
        Object.assign(context, await getBrowserLocationContext());
    }

    return context;
}

// 从发现结果中补齐当前事件对应的 Agent 描述，让 AIC/name/endpoint 都参与模板路由。
function getDiscoveredAgentForEvent(event) {
    const aic = String(event.aic || '').trim();
    const endpoint = String(event.endpoint || event.agent_endpoint || event.aip_endpoint || '').trim();
    const name = String(event.agent_name || event.agent || '').trim();
    return (state.currentIoaAgents || []).find(agent => {
        const agentEndpoint = String(agent.endpoint || agent.url || '').trim();
        return (aic && agent.aic === aic)
            || (endpoint && agentEndpoint === endpoint)
            || (name && agent.name === name);
    }) || null;
}

// 汇总 Agent 身份字段；这里不混入返回正文，避免 Finance 回复提到天气时被误判为天气模板。
function getAgentIdentityText(event) {
    const discovered = getDiscoveredAgentForEvent(event);
    return [
        event.aic,
        event.endpoint,
        event.agent_endpoint,
        event.aip_endpoint,
        event.agent_name,
        event.agent,
        discovered?.aic,
        discovered?.endpoint || discovered?.url,
        discovered?.name,
        discovered?.description,
        discovered?.skills ? JSON.stringify(discovered.skills) : ''
    ].filter(Boolean).join('\n');
}

// 尽量从 endpoint 中提取主机名，供本地/云端 Agent 展示文案使用。
function getEndpointHost(endpoint) {
    const value = String(endpoint || '').trim();
    if (!value) return '';
    try {
        return new URL(value).hostname.toLowerCase();
    } catch {
        const match = value.match(/https?:\/\/([^/:?#]+)/i);
        return match ? match[1].toLowerCase() : '';
    }
}

// 把 endpoint 压缩成适合天气应用主体展示的短标签，完整 URL 仍保留在底部 AIP 证据区。
function formatEndpointChannelLabel(endpoint) {
    const value = String(endpoint || '').trim();
    if (!value) return 'ACPs/AIP';
    try {
        const parsed = new URL(value);
        const host = parsed.hostname.replace(/^www\./i, '');
        const roleMatch = parsed.pathname.match(/\/api\/([^/]+)\/aip/i);
        const rpcMatch = parsed.pathname.match(/\/aip\/rpc/i);
        if (roleMatch?.[1]) return `ACPs/AIP · ${host} / ${roleMatch[1]}`;
        if (rpcMatch) return `ACPs/AIP · ${host} / rpc`;
        return `ACPs/AIP · ${host}`;
    } catch {
        return 'ACPs/AIP';
    }
}

// 仅把明确的本机地址判定为本地，局域网服务器仍按云端/服务器 Agent 口径展示。
function isLoopbackEndpointHost(host) {
    const normalized = String(host || '').toLowerCase().replace(/^\[|\]$/g, '');
    return normalized === 'localhost'
        || normalized === '::1'
        || normalized === '0:0:0:0:0:0:0:1'
        || normalized.startsWith('127.');
}

// 本轮 Demo 的“本地应用”白名单：只有天气能力可以打开本机天气 App，全量云端/普通 Agent 不进入应用弹窗。
function isLocalWeatherAppCandidate(event, content = '') {
    const hints = getAgentRenderHints(event);
    const identityText = getAgentIdentityText(event);
    const routeText = [
        hints.card_kind,
        hints.cardKind,
        hints.template_id,
        hints.templateId,
        hints.agent_app_preset,
        hints.agentAppPreset,
        event.skill_id,
        event.skillId,
        event.agent_name,
        event.agent,
        event.assigned_task
    ].filter(Boolean).join('\n');
    const blockedDomain = /酒店|美食|餐饮|旅游|旅行|行程|预订|财经|股票|证券|基金|游戏|车次|火车|高铁|hotel|food|travel|trip|finance|stock|gamer|game|train/i;
    if (blockedDomain.test(identityText)) return false;

    const hasWeatherCapability = /weather\.summary\.v1|weather\.query|wit-weather|天气查询智能体|本机天气|本地天气|天气应用智能体/i.test(`${identityText}\n${routeText}`);
    const hasWeatherResult = isWeatherAgentCard(event, content || routeText);
    return hasWeatherCapability && hasWeatherResult;
}

// 全屏 A2UI 应用仅服务于具备相应通用呈现契约的天气能力；其他智能体
// 留在聊天/流程卡中，避免按名称或提供方特殊展示。
function shouldOpenLocalAgentApp(event, preset, content = '') {
    return preset?.kind === 'weather' && isLocalWeatherAppCandidate(event, content);
}

// 非本地应用的 working/accepted 事件已有流程卡承载，避免再生成一张普通结果卡。
function shouldRenderInlineAgentResult(event, content = '') {
    const stateName = String(event.state || event.status || '').toLowerCase();
    if (['working', 'running', 'accepted'].includes(stateName)) return false;
    return Boolean(content)
        || ['completed', 'awaiting-completion', 'failed', 'rejected', 'canceled', 'cancelled', 'success'].includes(stateName);
}

// 根据 AIC、发现描述和 endpoint 给 Agent 加展示层来源标签，不改变 ACPs 原始返回。
function getAgentDeploymentScope(event) {
    const endpoint = event.endpoint || event.agent_endpoint || event.aip_endpoint || '';
    const host = getEndpointHost(endpoint);
    const identityText = getAgentIdentityText(event);
    const discovered = getDiscoveredAgentForEvent(event);
    // 部署位置只能由 Agent 自身身份决定，不能用 assigned_task/result 正文推断。
    // 任务正文可能带有其他能力的上下文，因此不用于判断智能体部署位置。
    const agentNameIdentity = [
        event.agent_name,
        event.agent,
        discovered?.name
    ].filter(Boolean).join('\n');
    const localWeatherIdentityHint = /wit-weather|天气查询智能体|本机天气|本地天气|天气应用智能体/i.test(agentNameIdentity);
    const cloudHint = /ioa\.pub|云端|cloud|registry|discovery/i.test(identityText) || /^https:\/\//i.test(String(endpoint || ''));

    if (localWeatherIdentityHint || (isLoopbackEndpointHost(host) && isWeatherAgentCard(event, ''))) {
        return {
            scope: 'local',
            location: '本地',
            foundTitle: '已找到本地应用智能体',
            selectionLabel: '本地应用智能体',
            evidenceLabel: '本地 Agent（本地 ACS）'
        };
    }

    return {
        scope: 'cloud',
        location: cloudHint ? '云端' : '云端',
        foundTitle: '已找到云端智能体',
        selectionLabel: '云端智能体',
        evidenceLabel: '云端 Agent（Registry/Discovery）'
    };
}

// 构造启动弹窗和 App HUD 里的人类可读调用句，明确 AIP 与 Agent 来源。
function formatAgentInvocationSummary(event, preset, options = {}) {
    const deployment = getAgentDeploymentScope(event);
    const agentName = getCapabilityAgentName(event, preset);
    const shortTitle = preset?.shortTitle || '';
    const appAgentLabel = shortTitle ? `${shortTitle}应用智能体` : '应用智能体';
    const isLocalWeatherApp = deployment.scope === 'local' && preset?.kind === 'weather';
    const state = String(event.state || event.status || '').toLowerCase();
    const hasResult = ['completed', 'awaiting-completion', 'success'].includes(state) && !options.loading;

    if (deployment.scope === 'local') {
        if (isLocalWeatherApp) {
            return hasResult
                ? '已通过 AIP 协议获取本机天气APP（支持 ACPs 的本地天气智能体）的返回结果'
                : '正在通过 AIP 协议调用本机天气APP（支持 ACPs 的本地天气智能体）...';
        }
        return hasResult
            ? `已通过 AIP 协议获取本地${appAgentLabel}（${agentName}）的返回结果`
            : `正在通过 AIP 协议调用本地${appAgentLabel}（${agentName}）...`;
    }

    return hasResult
        ? `已通过 AIP 协议获取云端 Agent ${agentName} 的返回结果`
        : `正在调用云端 Agent ${agentName}，并通过 AIP 协议启动${shortTitle || '智能体'}应用...`;
}

// 依据 AIC/endpoint/name 先给 Agent 绑定应用模板；Searcher 的天气任务再切到天气模板。
function resolveAgentAppPresetByIdentity(event, content = '') {
    const identityText = getAgentIdentityText(event);
    const matchedRule = AGENT_APP_IDENTITY_RULES.find(rule => rule.pattern.test(identityText));
    if (!matchedRule) return null;

    const identityPreset = getAgentAppPresetById(matchedRule.presetId);
    if (identityPreset?.kind === 'search' && isWeatherAgentCard(event, content)) {
        return getAgentAppPresetById('weather.summary.v1');
    }
    return identityPreset;
}

// 明显属于非天气领域的 Agent 不允许进入天气应用，即使返回正文或旧 hint 里出现“天气”。
function isKnownNonWeatherAgentEvent(event) {
    const identityText = getAgentIdentityText(event);
    const matchedRule = AGENT_APP_IDENTITY_RULES.find(rule => rule.pattern.test(identityText));
    const identityPreset = matchedRule ? getAgentAppPresetById(matchedRule.presetId) : null;
    return Boolean(identityPreset && !['weather', 'search', 'generic'].includes(identityPreset.kind));
}

// 汇总用于匹配预设的文本字段，覆盖后端 hint、Agent 元信息、任务文本和 AIP Markdown。
function getAgentAppPresetText(event, content) {
    const hints = getAgentRenderHints(event);
    return [
        hints.card_kind,
        hints.cardKind,
        hints.template_id,
        hints.templateId,
        hints.agent_app_preset,
        hints.agentAppPreset,
        event.skill_id,
        event.skillId,
        event.assigned_task,
        event.agent_name,
        event.agent,
        content
    ].filter(Boolean).join('\n');
}

// 使用正则匹配某个预设，避免每个模板重复拼接事件字段。
function matchAgentAppPresetText(event, content, pattern) {
    return pattern.test(getAgentAppPresetText(event, content));
}

// 按 id 读取预设；找不到时返回通用 Markdown 预设，保证渲染链路可降级。
function getAgentAppPresetById(presetId) {
    const normalized = String(presetId || '').trim();
    return AGENT_APP_PRESETS.find(preset => preset.id === normalized)
        || AGENT_APP_PRESETS.find(preset => preset.kind === normalized)
        || AGENT_APP_PRESETS.find(preset => preset.id === 'markdown.mc.v1');
}

// 从 render_hints 或事件字段读取后端显式给出的模板 id。
function getExplicitAgentAppPresetId(event) {
    const hints = getAgentRenderHints(event);
    return hints.template_id
        || hints.templateId
        || hints.agent_app_preset
        || hints.agentAppPreset
        || event.template_id
        || event.templateId;
}

// 解析某个 Agent 返回应该使用哪个应用预设；Agent 身份优先，其次才信任后端 hint 和内容关键词。
function resolveAgentAppPreset(event, content, options = {}) {
    const identityPreset = resolveAgentAppPresetByIdentity(event, content || '');
    if (identityPreset && identityPreset.id !== 'markdown.mc.v1') return identityPreset;

    const explicitPresetId = getExplicitAgentAppPresetId(event);
    if (explicitPresetId) {
        const explicitPreset = getAgentAppPresetById(explicitPresetId);
        if (explicitPreset?.kind === 'weather' && isKnownNonWeatherAgentEvent(event)) {
            return identityPreset || getAgentAppPresetById('markdown.mc.v1');
        }
        return explicitPreset;
    }
    if (identityPreset) return identityPreset;

    const matchedPreset = AGENT_APP_PRESETS.find(preset => {
        if (preset.autoOpen === false && !options.allowFallback) return false;
        return preset.matcher?.(event, content);
    });
    if (matchedPreset) return matchedPreset;

    if (options.allowFallback) return getAgentAppPresetById('markdown.mc.v1');
    return null;
}

// 根据用户输入预判本轮最可能启动的应用预设，用于执行调用阶段恢复上下文。
function detectAgentAppIntentPreset(text) {
    const normalized = String(text || '');
    return AGENT_APP_PRESETS.find(preset => {
        if (preset.autoOpen === false || !preset.keywords) return false;
        return preset.keywords.test(normalized);
    }) || null;
}

// 从待调用 Agent 列表中识别应用预设，解决“发现”和“执行”分成两次请求导致的前端状态丢失。
function detectAgentAppPresetFromAgents(agents) {
    for (const agent of Array.isArray(agents) ? agents : []) {
        const capabilityText = getAgentCapabilityText(agent);
        const preset = detectAgentAppIntentPreset(capabilityText);
        if (preset) return preset;
    }
    return null;
}

// 判断本次 Agent 返回是否应该由天气模板接管，避免天气结果继续按普通 Markdown 展示。
function isWeatherAgentCard(event, markdown) {
    if (isKnownNonWeatherAgentEvent(event)) return false;

    const hints = getAgentRenderHints(event);
    const hintText = [
        hints.card_kind,
        hints.cardKind,
        hints.template_id,
        hints.templateId,
        event.skill_id,
        event.skillId
    ].filter(Boolean).join(' ').toLowerCase();

    if (hintText.includes('weather') || hintText.includes('天气')) return true;

    const text = [
        event.assigned_task,
        event.agent_name,
        event.agent,
        markdown
    ].filter(Boolean).join('\n').toLowerCase();
    return /天气|气温|温度|降水|降雨|下雨|湿度|风速|weather|temperature|rain/.test(text);
}

// 从 Markdown 和普通文本中提取“键：值”指标，给天气模板填充稳定 data model。
function stripInlineMarkdown(text) {
    return String(text || '')
        .replace(/\*\*/g, '')
        .replace(/`/g, '')
        .replace(/\s+/g, ' ')
        .trim();
}

// 清理天气表格字段名中的图标和 Markdown 标记，保证不同 Wit 输出都能命中同一组 label。
function normalizeWeatherKey(text) {
    return stripInlineMarkdown(text)
        .replace(/[^\u4e00-\u9fa5A-Za-z0-9_%℃°/ -]/g, '')
        .trim();
}

// 清理天气字段值，避免把 Markdown 强调符号带进 A2UI 卡片。
function normalizeWeatherValue(text) {
    return stripInlineMarkdown(text)
        .replace(/^\|+|\|+$/g, '')
        .trim();
}

// “天气信息/情况”这类泛化词只表示句子主题，不能覆盖表格里的真实天气状况。
function isWeakWeatherMetricValue(value) {
    return /^(信息|情况|数据|结果|天气信息|实时天气信息|当前天气信息)$/.test(normalizeWeatherValue(value));
}

// 写入天气字段时保留先到的有效值，避免表头或空值覆盖真实指标。
function setWeatherValue(values, key, value) {
    const normalizedKey = normalizeWeatherKey(key);
    const normalizedValue = normalizeWeatherValue(value);
    if (!normalizedKey || !normalizedValue) return;
    if (!values[normalizedKey] || isWeakWeatherMetricValue(values[normalizedKey])) {
        values[normalizedKey] = normalizedValue;
    }
}

// 解析 Wit 常见 Markdown 表格，如“| 天气状况 | 多云 |”，补充到天气字段 map。
function parseWeatherTableLine(line, values) {
    const cells = String(line || '')
        .trim()
        .split('|')
        .map(cell => cell.trim())
        .filter(Boolean);

    if (cells.length < 2) return;
    if (cells.every(cell => /^:?-{2,}:?$/.test(cell))) return;
    const firstCell = normalizeWeatherKey(cells[0]);
    const secondCell = normalizeWeatherKey(cells[1]);
    if (/(项目|字段|指标)$/.test(firstCell) && /^(数据|数值|值|结果)$/.test(secondCell)) return;

    setWeatherValue(values, cells[0], cells[1]);
}

// 解析非表格的天气描述，兼容“气温在 18~22°C”“湿度约 55%”这类自然语言。
function parseWeatherProseLine(line, values) {
    const cleaned = stripInlineMarkdown(line)
        .replace(/^>+\s*/, '')
        .replace(/^#+\s*/, '')
        .replace(/^[-*]\s*/, '')
        .replace(/^\d+\.\s*/, '')
        .trim();
    if (!cleaned) return;

    const colonMatch = cleaned.match(/^([^:：]{1,18})[:：]\s*(.+)$/);
    if (colonMatch) {
        setWeatherValue(values, colonMatch[1], colonMatch[2]);
        return;
    }

    const prosePatterns = [
        ['气温范围', /(?:气温|温度)[^\d\-~～]{0,8}([+-]?\d+(?:\.\d+)?\s*(?:°C|℃|度)?\s*(?:[~～\-至到]\s*[+-]?\d+(?:\.\d+)?\s*(?:°C|℃|度)?)?)/i],
        ['天气状况', /(?:天气状况|天气|状况)\s*(?:[:：]|为|是)\s*([\u4e00-\u9fa5A-Za-z☀-⛿️，,、\s]{1,24})/i],
        ['湿度', /(?:相对湿度|湿度)[^\d]{0,8}(\d+(?:\.\d+)?\s*%)/i],
        ['风力', /(?:风力|风速)[^0-9一二三四五六七八九十东南西北偏级m/skmh公里小时]{0,8}([0-9一二三四五六七八九十~～\-至到级\s.]+(?:m\/s|km\/h|公里\/小时|级)?(?:（[^）]+）)?)/i],
        ['风向', /(?:风向)[^东南西北偏]{0,8}([东南西北偏风\s]{2,12})/i],
        ['降水', /(?:降水|降雨|下雨)[^无有小中大暴雨雪\d%]{0,8}(无降水|无降雨|不下雨|有降水|小雨|中雨|大雨|暴雨|雨夹雪|\d+(?:\.\d+)?\s*%)/i],
        ['数据源', /(?:数据源|来源)[：:\s]*(.+)$/i],
        ['更新时间', /(?:更新时间|更新于)[：:\s]*(.+)$/i]
    ];

    prosePatterns.some(([key, pattern]) => {
        const match = cleaned.match(pattern);
        if (!match?.[1]) return false;
        setWeatherValue(values, key, match[1]);
        return true;
    });
}

function parseWeatherKeyValues(markdown) {
    const values = {};
    String(markdown || '').split(/\r?\n/).forEach(line => {
        if (/^\s*\|/.test(line)) {
            parseWeatherTableLine(line, values);
            return;
        }

        parseWeatherProseLine(line, values);
    });
    return values;
}

// 按多个候选中文/英文标签查找天气字段，兼容不同 Agent 的 Markdown 写法。
function findWeatherValue(values, labels) {
    const normalizedEntries = Object.entries(values).map(([key, value]) => ({
        key: key.toLowerCase(),
        value
    }));
    for (const label of labels) {
        const normalizedLabel = label.toLowerCase();
        const exact = normalizedEntries.find(entry => entry.key === normalizedLabel);
        if (exact) return exact.value;
        const partial = normalizedEntries.find(entry => entry.key.includes(normalizedLabel));
        if (partial) return partial.value;
    }
    return '';
}

// 城市/地点字段不能用模糊包含，否则“返回位置”会被误当作天气地点。
function findWeatherExactValue(values, labels) {
    const normalizedEntries = Object.entries(values).map(([key, value]) => ({
        key: key.toLowerCase(),
        value
    }));
    for (const label of labels) {
        const normalizedLabel = label.toLowerCase();
        const exact = normalizedEntries.find(entry => entry.key === normalizedLabel);
        if (exact) return exact.value;
    }
    return '';
}

// 将口语查询里夹带的“查一下、现在、当前、天气”等词从城市候选中剥掉。
function cleanWeatherCityCandidate(candidate) {
    let city = stripInlineMarkdown(candidate)
        .replace(/[，,。！？?：:；;、]+$/g, '')
        .trim();
    if (!city) return '';

    const weatherIntentMatch = city.match(/(?:请|麻烦|帮我|帮忙|想知道|我想知道|查一下|查询|查|查看|看看|调用|使用|获取|提供|生成|展示|呈现|返回|并返回|一下|当前|实时|现在|今天|明天|后天)*\s*([\u4e00-\u9fa5]{2,12}(?:\s*\([A-Za-z\s-]+\))?)\s*(?:的)?(?:当前|实时|现在|今天|明天|后天)?(?:天气|气温|温度|气象|情况|信息)/);
    if (weatherIntentMatch?.[1]) {
        city = weatherIntentMatch[1].trim();
    }

    city = city
        .replace(/^(?:这|未来|最近)?[一二两三四五六七八九十\d]+天(?:内)?/g, '')
        .replace(/^(?:本周|周末)/g, '')
        .replace(/^(?:请|麻烦|帮我|帮忙|想知道|我想知道|查一下|查询|查|查看|看看|调用|使用|获取|提供|生成|展示|呈现|返回|并返回|一下)+/g, '')
        .replace(/(?:的)?(?:天气|气温|温度|情况|信息|怎么样|如何)+$/g, '')
        .replace(/(?:现在|当前|实时|此刻|今天|明天|后天)+$/g, '')
        .replace(/^(?:现在|当前|实时|今天|明天|后天)+/g, '')
        .replace(/^(?:的|并|并返回|返回)+/g, '')
        .replace(/(?:的|并|返回)+$/g, '')
        .trim();

    if (/^(?:查询|查看|看看|调用|使用|获取|生成|返回|提供|执行|任务|功能|天气查询功能|当前|实时|现在|天气查询|天气智能体|智能体|数据|天气数据|指标|数值|结果|返回结果|城市|哪个城市|哪座城市|具体城市)$/.test(city)) return '';
    if (/^[\u4e00-\u9fa5]{2,12}(?:\s*\([A-Za-z\s-]+\))?$/.test(city)) return city;
    return '';
}

// 从一段自然语言查询中直接抽取城市，优先处理用户原始输入，避免 AIP 分配任务里的动词短语污染标题。
function inferWeatherCityFromText(text) {
    const source = stripInlineMarkdown(text);
    if (!source) return '';
    const lines = source.split(/[\r\n]+/).map(line => line.trim()).filter(Boolean);
    const patterns = [
        /(?:用户原始请求|用户补充|补充|原始请求)[:：]\s*([^。；\n]+)/,
        /(?:获取|查询|查看|看看|了解|返回|提供|生成)\s*([\u4e00-\u9fa5]{2,12}(?:\s*\([A-Za-z\s-]+\))?)\s*(?:的)?(?:当前|实时|现在|今天|明天|后天)?(?:天气|气温|温度|气象|情况|信息|数据)/,
        /(?:请|麻烦|帮我|帮忙|想知道|我想知道|查一下|查询|查|查看|看看|调用|使用|获取|提供|生成|展示|呈现|返回|并返回|一下)?\s*([\u4e00-\u9fa5]{2,12}(?:\s*\([A-Za-z\s-]+\))?)\s*(?:的)?(?:当前|实时|现在|今天|明天|后天)?(?:天气|气温|温度|气象|情况|信息)/,
        /(?:天气|气温|温度|气象)\s*(?:在|for|of)?\s*([\u4e00-\u9fa5]{2,12}(?:\s*\([A-Za-z\s-]+\))?)/i,
        /(?:in|for)\s+([A-Za-z][A-Za-z\s-]{1,40})\s+(?:weather|temperature)/i
    ];
    for (const line of lines) {
        for (const pattern of patterns) {
            const match = line.match(pattern);
            const city = cleanWeatherCityCandidate(match?.[1]);
            if (city) return city;
        }
    }
    return '';
}

// 清理天气标题中的经纬度等辅助信息，标题只保留地点和天气语义，避免手机窄屏被坐标挤成多行。
function cleanWeatherDisplayTitle(title) {
    return stripInlineMarkdown(title)
        .replace(/[（(]\s*[-+]?\d+(?:\.\d+)?\s*°?\s*[EW东西]?\s*[,，]\s*[-+]?\d+(?:\.\d+)?\s*°?\s*[NS南北]?\s*[）)]/gi, '')
        .replace(/\s+/g, ' ')
        .trim();
}

// 提取第一段适合作为天气摘要的纯文本，避免把完整 Markdown 直接呈现在主卡片里。
function extractWeatherSummary(markdown) {
    const lines = String(markdown || '')
        .split(/\r?\n/)
        .map(line => line.trim())
        .filter(Boolean);
    const quote = lines.find(line => line.startsWith('>'));
    if (quote) return quote.replace(/^>\s*/, '').trim();

    const prose = lines.find(line => {
        if (/^#{1,6}\s/.test(line)) return false;
        if (/^```/.test(line)) return false;
        if (/^\|/.test(line)) return false;
        if (/^[-*]\s*[^:：]{1,16}[:：]/.test(line)) return false;
        return /天气|气温|温度|多云|晴|雨|阴|风|湿度|weather|temperature/i.test(line);
    });
    return prose ? prose.replace(/^[-*]\s*/, '').trim() : '';
}

// 从用户任务或返回文本中尽量识别城市名；识别不到时保持空值，由 UI 显示查询任务。
function inferWeatherCity(event, markdown, values) {
    const explicit = findWeatherExactValue(values, ['城市', '地点', '地区', '位置', '查询地点', 'city', 'location']);
    if (explicit) return cleanWeatherCityCandidate(explicit) || explicit;

    const continueInput = event.continue_input || event.continueInput || event.user_input || event.userInput || '';
    if (continueInput && !/当前位置|定位|经纬度|location/i.test(continueInput)) {
        const fromContinueInput = inferWeatherCityFromText(continueInput) || cleanWeatherCityCandidate(continueInput);
        if (fromContinueInput) return fromContinueInput;
    }

    const queryText = event.user_query_context || event.userQueryContext || event.user_query || event.userQuery || state.currentTaskIntent.queryText;
    const fromUserQuery = inferWeatherCityFromText(queryText);
    if (fromUserQuery) return fromUserQuery;

    const fromAssignedTask = inferWeatherCityFromText(event.assigned_task);
    if (fromAssignedTask) return fromAssignedTask;

    const text = [queryText, event.assigned_task, markdown].filter(Boolean).join('\n');
    const patterns = [
        /^#{1,6}\s*[^\u4e00-\u9fa5A-Za-z]*([\u4e00-\u9fa5]{2,12}(?:\s*\([A-Za-z\s-]+\))?)\s*(?:[·｜|\-]\s*)?(?:实时|当前|现在)?(?:天气|气温|温度|气象)/m,
        /\*\*([\u4e00-\u9fa5]{2,12}(?:\s*\([A-Za-z\s-]+\))?)\s*(?:当前|实时|现在)?(?:天气|气温|温度|气象)?\*\*/,
        /以下是(?:为您查询到的)?\s*([\u4e00-\u9fa5]{2,12})(?:当前|实时|现在|区当前|市当前|的当前|的实时|的)?(?:（[^）]+）)?(?:实时)?(?:天气|气温|温度|气象|信息)/,
        /\*\*([\u4e00-\u9fa5]{2,8})\*\*[^。；\n]*(?:天气|气温|温度)/,
        /以下是\s*([\u4e00-\u9fa5]{2,8})(?:目前|当前|实时)/,
        /以下是\s*([\u4e00-\u9fa5]{2,8})\s*(?:当前|实时)/,
        /([\u4e00-\u9fa5]{2,8})(?:当前|实时)(?:天气|气温|温度)/,
        /(?:明天|今天|后天|本周|周末)?\s*([\u4e00-\u9fa5]{2,8})(?:的)?(?:天气|气温|温度)/,
        /(?:查询|查看|看看)\s*([\u4e00-\u9fa5]{2,8})(?:的)?(?:天气|气温|温度)/,
        /(?:in|for)\s+([A-Za-z][A-Za-z\s-]{1,40})\s+(?:weather|temperature)/i
    ];
    const blockedCandidates = /^(查询|查看|看看|一下|任务|当前|实时|现在|天气状况|实时气温|当前气温|体感温度|相对湿度|风速|温度|气温|湿度|天气|风况|数据|天气数据|指标|数值|结果|返回结果|城市|哪个城市|哪座城市|具体城市)$/;
    for (const pattern of patterns) {
        const match = text.match(pattern);
        const city = cleanWeatherCityCandidate(match?.[1]);
        if (city && !blockedCandidates.test(city)) return city;
    }
    return '';
}

// 从返回正文或任务上下文里识别日期，给天气应用副标题使用。
function inferWeatherDate(event, markdown, values) {
    const explicit = findWeatherValue(values, ['日期', '时间', '预报日期', 'date']);
    if (explicit) return explicit;

    const queryText = event.user_query_context || event.userQueryContext || event.user_query || event.userQuery || state.currentTaskIntent.queryText;
    const text = [queryText, event.assigned_task, markdown].filter(Boolean).join('\n');
    const absolute = text.match(/(?:当前|实时|今天|明天|后天)?[（(]?(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2})[）)]?/);
    if (absolute?.[1]) return absolute[1];
    const relative = text.match(/(今天|明天|后天|当前|实时|现在)/);
    return relative?.[1] || '';
}

// 风况通常会被拆成风向和风力，组合后比单独显示其中一个更适合演示。
function buildWeatherWindValue(values) {
    const speed = findWeatherValue(values, ['风速', 'wind speed']);
    const force = findWeatherValue(values, ['风力']);
    const direction = findWeatherValue(values, ['风向', 'wind direction']);
    const generic = findWeatherValue(values, ['风况', 'wind']);
    const parts = [direction, speed || force].filter(Boolean);
    return parts.length ? Array.from(new Set(parts)).join(' · ') : generic;
}

// 识别文本中出现的已知天气服务提供方，返回去重前的候选名称列表。
function detectKnownWeatherSources(text) {
    return [
        ['OpenWeatherMap', /OpenWeatherMap/i],
        ['Open-Meteo', /Open[-\s]?Meteo/i],
        ['中国天气网', /中国天气网/],
        ['中国气象公开信息', /中国气象公开信息|中国气象/],
        ['天气网', /天气网/]
    ]
        .filter(([, pattern]) => pattern.test(text))
        .map(([label]) => label);
}

// 从 Wit 返回正文里抽取真实天气数据源；正文没有声明时，再降级读取 Agent 能力说明。
function inferWeatherDataSource(event, markdown, values) {
    const explicit = findWeatherValue(values, ['数据源', '来源', 'source']);
    if (explicit) return { label: explicit, confidence: 'result' };

    const text = stripInlineMarkdown(markdown);
    const knownSources = detectKnownWeatherSources(text);
    if (knownSources.length > 0) {
        return { label: Array.from(new Set(knownSources)).join(' / '), confidence: 'result' };
    }

    const sourceMatch = text.match(/(?:基于|来源于|来自|数据来自)([^。；\n]{2,40})/);
    if (sourceMatch?.[1]) return { label: sourceMatch[1].trim(), confidence: 'result' };

    const eventAgentName = event.agent_name || event.agent || event.name || '';
    const matchedDiscoveryAgents = (state.currentIoaAgents || [])
        .filter(agent => !eventAgentName || agent.name === eventAgentName || String(eventAgentName).includes(agent.name))
        .map(agent => [agent.name, agent.description, agent.assigned_task, agent.endpoint].filter(Boolean).join('\n'));
    const agentContext = [
        event.description,
        event.agent_description,
        event.agent?.description,
        event.assigned_task,
        event.agent_name,
        event.agent,
        ...matchedDiscoveryAgents
    ].filter(Boolean).join('\n');
    const contextSources = detectKnownWeatherSources(stripInlineMarkdown(agentContext));
    if (contextSources.length > 0) {
        return { label: Array.from(new Set(contextSources)).join(' / '), confidence: 'agent' };
    }
    return { label: '', confidence: 'missing' };
}

// 记录本次结果的协议返回路径，和天气数据源分开展示，方便演示 ACPs 证据链。
function inferWeatherTransportSource(event) {
    const endpoint = event.endpoint || event.agent_endpoint || event.aip_endpoint || '';
    return formatEndpointChannelLabel(endpoint);
}

// 把 provider 名转换成用户能理解的天气数据文案，避免看起来像另一个 App 名称。
function formatWeatherDataSourceLabel(weather) {
    const raw = String(weather?.dataSource || '').trim();
    if (!raw) return '由天气智能体返回';
    const cleaned = raw
        .replace(/[（(]\s*Agent\s*能力说明\s*[）)]/gi, '')
        .replace(/（\s*Agent\s*能力说明\s*）/g, '')
        .trim();
    const labels = cleaned.split(/\s*\/\s*/).map(item => {
        if (/^OpenWeatherMap$/i.test(item)) return 'OpenWeatherMap 天气 API';
        if (/^Open[-\s]?Meteo$/i.test(item)) return 'Open-Meteo 天气 API';
        if (/中国天气网|天气网|中国气象/.test(item)) return item;
        return item;
    }).filter(Boolean);
    return labels.join(' / ') || '由天气智能体返回';
}

// 天气卡右上角只放短状态，不直接展示 provider 或长 URL。
function getWeatherToplineSourceLabel(weather) {
    if (weather?.needsInput) return '等待城市信息';
    if (weather?.loading) return '云端天气智能体';
    if (weather?.dataSource) return '天气数据源';
    return 'AIP 返回结果';
}

// 将 Markdown 中已识别到的天气说明收敛为 A2UI 文本行，避免解析失败时出现空白应用。
function extractWeatherDetailLines(markdown, values) {
    const detailLines = [];
    Object.entries(values).forEach(([key, value]) => {
        if (/(项目|字段|指标)$/.test(key) && /^(数据|数值|值|结果)$/.test(value)) return;
        if (/^(城市|地点|地区|位置|查询地点)$/i.test(key)) return;
        detailLines.push(`${key}：${value}`);
    });

    if (detailLines.length === 0) {
        String(markdown || '').split(/\r?\n/).forEach(line => {
            const cleaned = stripInlineMarkdown(line)
                .replace(/^>+\s*/, '')
                .replace(/^#+\s*/, '')
                .replace(/^[-*]\s*/, '')
                .replace(/^\d+\.\s*/, '')
                .trim();
            if (!cleaned) return;
            if (/^\|?[:\- ]+\|?$/.test(cleaned)) return;
            if (/天气|气温|温度|湿度|风|降水|降雨|体感|空气|着装|舒适/.test(cleaned)) {
                detailLines.push(cleaned);
            }
        });
    }

    return Array.from(new Set(detailLines)).slice(0, 8);
}

// 读取 Markdown 中第一个 json-chart，天气卡可复用它显示温度或降水趋势。
function extractWeatherChart(markdown) {
    const chartPart = parseAgentCardParts(markdown).find(part => part.type === 'chart');
    return chartPart ? parseJsonChart(chartPart.content) : null;
}

// 解析 Wit 可能返回的未来几天预报表格；当前结果没有预报时返回空数组，不影响实时天气主卡。
function extractWeatherForecastRows(markdown) {
    const rows = String(markdown || '')
        .split(/\r?\n/)
        .map(line => line.trim())
        .filter(line => line.startsWith('|'))
        .map(line => line.replace(/^\||\|$/g, '').split('|').map(cell => stripInlineMarkdown(cell).trim()));
    const forecast = [];
    let header = null;
    rows.forEach(cells => {
        if (cells.length < 2) return;
        if (cells.every(cell => /^:?-{3,}:?$/.test(cell))) return;
        const joined = cells.join('');
        if (!header && /日期|时间|day|date/i.test(joined) && /天气|温度|气温|weather|temperature/i.test(joined)) {
            header = cells;
            return;
        }
        if (!header) return;
        const rowMap = {};
        header.forEach((name, index) => {
            rowMap[name] = cells[index] || '';
        });
        const date = findWeatherValue(rowMap, ['日期', '时间', 'date', 'day']) || cells[0] || '';
        const condition = findWeatherValue(rowMap, ['天气', '天气状况', 'weather', 'condition']) || cells[1] || '';
        const low = findWeatherValue(rowMap, ['最低温', '最低气温', 'low']);
        const high = findWeatherValue(rowMap, ['最高温', '最高气温', 'high']);
        const temperature = findWeatherValue(rowMap, ['温度', '气温', 'temperature'])
            || (low && high ? `${low}~${high}` : high || low)
            || cells[2]
            || '';
        const wind = findWeatherValue(rowMap, ['风况', '风力', '风速', 'wind']) || '';
        if (date && (condition || temperature || wind)) {
            forecast.push({ date, condition, temperature, wind });
        }
    });
    if (forecast.length > 0) return forecast.slice(0, 5);

    // 不要求天气 Agent 固定使用表格。逐日标题、列表或自然语言只要同一行含日期，
    // 就按日期建立独立记录，避免第二天字段覆盖第一天主卡。
    String(markdown || '').split(/\r?\n/).forEach(line => {
        const cleaned = stripInlineMarkdown(line)
            .replace(/^>+\s*/, '')
            .replace(/^#{1,6}\s*/, '')
            .replace(/^[-*]\s*/, '')
            .trim();
        const dateMatch = cleaned.match(/((?:\d{4}年)?\d{1,2}月\d{1,2}日(?:[（(]?周[一二三四五六日天][）)]?)?)/);
        if (!dateMatch) return;
        const temperatureMatch = cleaned.match(/(-?\d+(?:\.\d+)?\s*(?:℃|°C)?)\s*[~～—–-]\s*(-?\d+(?:\.\d+)?\s*(?:℃|°C)?)/i);
        const singleTemperature = cleaned.match(/(?:温度|气温)[：:\s]*(-?\d+(?:\.\d+)?\s*(?:℃|°C))/i);
        const windMatch = cleaned.match(/((?:东|南|西|北|东北|东南|西北|西南)?风[^，,；;。]{0,16})/);
        let condition = cleaned
            .replace(dateMatch[0], '')
            .replace(temperatureMatch?.[0] || singleTemperature?.[0] || '', '')
            .replace(windMatch?.[0] || '', '')
            .replace(/^[：:\s|]+|[|，,；;。\s]+$/g, '')
            .replace(/^(?:天气状况|天气|状况)[：:\s]*/i, '');
        const conditionMatch = condition.match(/(?:晴|多云|阴|雾|霾|小雨|中雨|大雨|暴雨|雷阵雨|阵雨|雨夹雪|小雪|中雪|大雪)[^，,；;。|\d℃]{0,12}/);
        if (conditionMatch) condition = conditionMatch[0].trim();
        const temperature = temperatureMatch
            ? `${temperatureMatch[1]}~${temperatureMatch[2]}`
            : (singleTemperature?.[1] || '');
        if (condition || temperature || windMatch?.[1]) {
            forecast.push({
                date: dateMatch[1],
                condition,
                temperature,
                wind: windMatch?.[1] || ''
            });
        }
    });
    return forecast.slice(0, 5);
}

// 把 AIP 文本结果转换成天气卡 data model；缺字段时保留可读 fallback。
function buildWeatherCardData(event, markdown) {
    const values = parseWeatherKeyValues(markdown);
    const forecast = extractWeatherForecastRows(markdown);
    const primaryForecast = forecast[0] || {};
    const heading = String(markdown || '').match(/^#\s+(.+)$/m)?.[1]?.trim() || '';
    const displayHeading = cleanWeatherDisplayTitle(heading);
    const headingLooksWeatherSpecific = displayHeading
        && detectWeatherTaskIntent(displayHeading)
        && !/助手|就绪|searcher|搜索|学术|综合|数据|返回结果/i.test(displayHeading);
    const stateName = String(event.state || event.status || 'working').toLowerCase();
    const needsInput = isAgentAwaitingInput(event);
    const loading = !needsInput && (stateName === 'working' || stateName === 'accepted' || event.status === 'running');
    const city = inferWeatherCity(event, markdown, values);
    let summary = loading
        ? '正在通过 ACPs/AIP 调用天气能力，结果会在同一张卡片中实时更新。'
        : needsInput
            ? (extractWeatherSummary(markdown) || displayHeading || '天气智能体需要补充城市或当前位置后才能继续查询。')
            : (extractWeatherSummary(markdown) || displayHeading || '已收到天气查询结果。');

    if (!loading && !needsInput && primaryForecast.date) {
        summary = [primaryForecast.date, primaryForecast.condition, primaryForecast.temperature]
            .filter(Boolean)
            .join(' · ');
    }

    const high = findWeatherValue(values, ['最高温', '最高气温', 'high']);
    const low = findWeatherValue(values, ['最低温', '最低气温', 'low']);
    const temperature = primaryForecast.temperature
        || findWeatherValue(values, ['实时气温', '当前气温', '当前温度', '温度', '气温', '体感温度', 'temperature'])
        || (high && low ? `${low} - ${high}` : high || low);
    const weatherDate = primaryForecast.date || inferWeatherDate(event, markdown, values);
    const dataSourceInfo = inferWeatherDataSource(event, markdown, values);

    return {
        loading,
        needsInput,
        useAppLevelInput: true,
        inputPrompt: needsInput ? summary : '',
        cardRef: getAgentResultCardDomId(event),
        canUseLocation: needsInput && Boolean(navigator.geolocation),
        title: needsInput ? '天气应用' : (city ? `${city}天气` : (headingLooksWeatherSpecific ? displayHeading : '天气应用')),
        city,
        date: weatherDate,
        condition: primaryForecast.condition || findWeatherValue(values, ['天气状况', '天气', '状况', '概况', 'condition']) || '',
        summary,
        temperature,
        precipitation: findWeatherValue(values, ['降水概率', '降雨概率', '降水', '降雨', 'precipitation', 'rain']),
        humidity: findWeatherValue(values, ['相对湿度', '湿度', 'humidity']),
        wind: buildWeatherWindValue(values),
        source: dataSourceInfo.label || 'Wit Agent / AIP TaskResult',
        dataSource: dataSourceInfo.label,
        dataSourceConfidence: dataSourceInfo.confidence,
        transportSource: inferWeatherTransportSource(event),
        updatedAt: findWeatherValue(values, ['更新时间', '更新于', 'updated', 'update time']),
        query: event.assigned_task || '',
        details: extractWeatherDetailLines(markdown, values),
        chart: extractWeatherChart(markdown),
        forecast
    };
}

// Hero 区只显示主温度，体感温度等长补充信息留在指标区，防止标题被挤成逐字换行。
function formatWeatherHeroTemperature(weather) {
    if (!weather.temperature) return weather.loading ? '查询中' : '--';
    const primary = String(weather.temperature).split(/[（(]/)[0].trim();
    return primary || String(weather.temperature);
}

// 清理单个城市素材候选词，避免把后端任务上下文或天气口语一并带入素材搜索。
function cleanWeatherCityAssetCandidate(value) {
    return String(value || '')
        .split(/叮当上下文|用户原始请求|位置状态|语言|时区|Agent|Endpoint/i)[0]
        .replace(/[（(][^）)]*[）)]/g, ' ')
        .replace(/天气应用|天气查询功能|天气查询|天气|实时|当前|现在|查询|查一下|调用|使用|获取|生成|功能|一下/g, ' ')
        .replace(/[·|/\\]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

// 判断查询是否已经细到区县旗等行政区；这类查询不能直接套父级城市图册。
function isWeatherDistrictLevelQuery(value) {
    const clean = cleanWeatherCityAssetCandidate(value).replace(/\s+/g, '');
    if (!clean) return false;
    return /(?:自治县|自治旗|新区|开发区|示范区|林区|特区|矿区|区|县|旗)$/.test(clean)
        && !/^(?:市区|城区|景区|社区)$/.test(clean);
}

// 去掉省、市等父级前缀，尽量把“北京市海淀区 / 西安市雁塔区”归一成“海淀区 / 雁塔区”。
function stripWeatherParentAdminPrefix(value) {
    let clean = cleanWeatherCityAssetCandidate(value).replace(/\s+/g, '');
    if (!clean) return '';
    clean = clean.replace(/^(?:中国|中华人民共和国)/, '');
    for (let index = 0; index < 3; index += 1) {
        const next = clean
            .replace(/^(?:北京市|北京|上海市|上海|天津市|天津|重庆市|重庆)/, '')
            .replace(/^[\u4e00-\u9fa5]{2,12}(?:省|自治区|壮族自治区|回族自治区|维吾尔自治区|特别行政区)/, '')
            .replace(/^[\u4e00-\u9fa5]{2,12}(?:市|自治州|地区|盟)/, '');
        if (next === clean) break;
        clean = next;
    }
    return clean;
}

// 为区县级真实图片检索生成更精确的百科标题候选，避免“北京海淀区”只搜到北京市。
function buildWeatherDistrictTitleCandidates(query) {
    const clean = cleanWeatherCityAssetCandidate(query).replace(/\s+/g, '');
    if (!clean) return [];

    const candidates = [clean];
    const stripped = stripWeatherParentAdminPrefix(clean);
    if (stripped && stripped !== clean && isWeatherDistrictLevelQuery(stripped)) {
        candidates.unshift(stripped);
    }

    const directMunicipality = clean.match(/^(?:北京(?:市)?|上海(?:市)?|天津(?:市)?|重庆(?:市)?)([\u4e00-\u9fa5]{2,8})(?:区|县)?$/);
    if (directMunicipality?.[1]) {
        const district = directMunicipality[1].replace(/(?:区|县)$/, '');
        if (district) candidates.unshift(`${district}区`);
    }

    return Array.from(new Set(candidates.filter(item => isWeatherDistrictLevelQuery(item))));
}

// 规整城市素材查询词，优先使用结构化城市名，最后才退回原始 query。
function normalizeWeatherCityAssetQuery(weather) {
    const queryCity = inferWeatherCityFromText(weather.query || '');
    const districtCandidates = [weather.city, weather.title, queryCity, weather.query]
        .flatMap(buildWeatherDistrictTitleCandidates)
        .filter(Boolean);
    const candidates = districtCandidates.length > 0 ? districtCandidates : [weather.city, weather.title, queryCity, weather.query]
        .map(cleanWeatherCityAssetCandidate)
        .filter(Boolean);
    const firstUseful = candidates.find(item => !/智能体应用|应用$/.test(item)) || candidates[0] || '';
    const parts = firstUseful
        .split(/[，,;；]/)
        .map(part => part.trim())
        .filter(Boolean);
    return (parts.join(' ') || firstUseful).slice(0, 40);
}

// 给本地城市图册 URL 追加资源版本，确保 WebView 不复用旧 APK/旧浏览器缓存中的英文底字 SVG。
function versionWeatherCityAssetUrl(url) {
    const value = String(url || '').trim();
    if (!value || !/^assets\/weather-cities\//.test(value)) return value;
    const joiner = value.includes('?') ? '&' : '?';
    return `${value}${joiner}v=${encodeURIComponent(WEATHER_CITY_ASSET_VERSION)}`;
}

// 根据城市名匹配本地精选素材；不命中时返回空对象，让远程 provider 或 CSS 兜底接管。
function resolveLocalWeatherCityAsset(weather) {
    const queryCity = inferWeatherCityFromText(weather.query || '');
    const priorityTexts = [weather.city, weather.title, queryCity, weather.query]
        .map(cleanWeatherCityAssetCandidate)
        .filter(Boolean);
    if (priorityTexts.some(isWeatherDistrictLevelQuery)) {
        return WEATHER_CITY_VISUAL_CONFIG.localAssets.find(item => {
            const titles = [item.title].concat(item.wikiTitles || []).map(value => String(value || '').replace(/\s+/g, ''));
            return priorityTexts.some(text => titles.includes(text.replace(/\s+/g, '')));
        }) || null;
    }
    for (const text of priorityTexts) {
        const matched = WEATHER_CITY_VISUAL_CONFIG.localAssets.find(item => item.pattern.test(text));
        if (matched) return matched;
    }
    const fallbackText = [weather.summary, weather.details?.join('\n')]
        .filter(Boolean)
        .map(cleanWeatherCityAssetCandidate)
        .join('\n');
    return WEATHER_CITY_VISUAL_CONFIG.localAssets.find(item => item.pattern.test(fallbackText)) || null;
}

// 根据城市 slug 生成本地真实地标照片候选；每城固定维护三张精选 JPG，首张是人工选出的首屏图。
function buildWeatherLandmarkPhotoUrls(slug) {
    const safeSlug = String(slug || '').trim().toLowerCase();
    if (!/^[a-z0-9-]+$/.test(safeSlug)) return [];
    return [1, 2, 3].map(index => {
        const rank = String(index).padStart(2, '0');
        return `assets/weather-cities/landmark-photos/${safeSlug}/${safeSlug}_${rank}.jpg`;
    });
}

// 判断候选列表是否包含本地真实图册；只有这种情况下才让本地照片优先于远程图库。
function hasWeatherLandmarkPhotoCandidate(urls) {
    return (urls || []).some(url => /\/landmark-photos\//.test(String(url || '')));
}

// 把素材配置里的本地真实图、主 URL、备用 URL 和全局兜底 URL 合并去重，保证任一城市至少有可尝试的本地候选。
function normalizeWeatherAssetUrlList(asset) {
    const urls = []
        .concat(buildWeatherLandmarkPhotoUrls(asset?.photoSlug))
        .concat(asset?.urls || [])
        .concat(asset?.url || [])
        .concat(asset?.fallbackUrls || [])
        .concat(WEATHER_CITY_VISUAL_CONFIG.fallbackUrls || [])
        .map(versionWeatherCityAssetUrl)
        .map(normalizeWeatherImageUrl)
        .filter(Boolean);
    return Array.from(new Set(urls));
}

// 把 URL 数组压进 DOM 属性，hydrate 阶段可逐个探测，不因第一张图失效就放弃城市素材。
function encodeWeatherAssetUrls(urls) {
    if (!Array.isArray(urls) || urls.length === 0) return '';
    return encodeURIComponent(JSON.stringify(urls));
}

// 从 DOM 属性恢复 URL 数组；解析失败时返回空数组，让远程 provider 继续兜底。
function decodeWeatherAssetUrls(value) {
    if (!value) return [];
    try {
        const parsed = JSON.parse(decodeURIComponent(value));
        return Array.isArray(parsed) ? parsed.map(normalizeWeatherImageUrl).filter(Boolean) : [];
    } catch {
        return [];
    }
}

// 标准化远程图片 URL，避免把无效或危险协议写入 CSS 变量。
function normalizeWeatherImageUrl(url) {
    const value = String(url || '').trim();
    if (!value) return '';
    if (value.startsWith('//')) return `https:${value}`;
    try {
        // 叮当部署在 /dingdang-full/ 子路径，使用 origin 会把 assets/... 错解为 /assets/... 并得到 404。
        const parsed = new URL(value, document.baseURI || window.location.href);
        if (['http:', 'https:', 'data:'].includes(parsed.protocol)) return parsed.href;
    } catch {}
    return '';
}

// 带超时的请求封装，避免大陆网络访问远程素材服务慢或不可达时卡住天气应用。
function fetchWeatherAssetWithTimeout(url, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { signal: controller.signal }).finally(() => clearTimeout(timer));
}

// 先确认图片本身可以加载，再写入 CSS 背景；不可达时走本地 CSS 城市光影兜底。
function preloadWeatherImage(url, timeoutMs = WEATHER_CITY_VISUAL_CONFIG.imageTimeoutMs) {
    return new Promise(resolve => {
        const safeUrl = normalizeWeatherImageUrl(url);
        if (!safeUrl) {
            resolve(false);
            return;
        }
        const image = new Image();
        const timer = setTimeout(() => {
            image.onload = null;
            image.onerror = null;
            resolve(false);
        }, timeoutMs);
        image.onload = () => {
            clearTimeout(timer);
            resolve(true);
        };
        image.onerror = () => {
            clearTimeout(timer);
            resolve(false);
        };
        image.referrerPolicy = 'no-referrer';
        image.decoding = 'async';
        image.src = safeUrl;
    });
}

// 图片预加载在部分 headless/WebView 环境下对 SVG 回调不稳定；同源本地素材再用 fetch 校验一次。
async function verifyWeatherImageReachable(url, timeoutMs = WEATHER_CITY_VISUAL_CONFIG.imageTimeoutMs) {
    const safeUrl = normalizeWeatherImageUrl(url);
    if (!safeUrl) return false;
    const imageLoaded = await preloadWeatherImage(safeUrl, timeoutMs);
    if (imageLoaded) return true;
    try {
        const target = new URL(safeUrl, window.location.origin);
        if (target.origin !== window.location.origin) return false;
        const response = await fetchWeatherAssetWithTimeout(target.href, timeoutMs);
        return response.ok;
    } catch {
        return false;
    }
}

// 从一组 URL 中选择第一张真正可加载的图片；本地图册、备用 URL 和远程图片都走同一条健康检查。
async function resolveReachableWeatherCityAsset(urls, meta = {}) {
    for (const url of urls) {
        const safeUrl = normalizeWeatherImageUrl(url);
        if (!safeUrl) continue;
        const reachable = await verifyWeatherImageReachable(safeUrl);
        if (reachable) {
            return {
                url: safeUrl,
                label: meta.label || WEATHER_CITY_VISUAL_CONFIG.providerLabel,
                title: meta.title || '',
                provider: meta.provider || 'local-album'
            };
        }
    }
    return null;
}

// 给开放百科查询生成城市条目候选，优先命中“北京市/上海市”等行政区条目，避免全文搜索命中人物或影视条目。
function buildWikimediaCityTitleCandidates(query) {
    const cleanQuery = cleanWeatherCityAssetCandidate(query).replace(/\s+/g, '');
    const districtTitles = buildWeatherDistrictTitleCandidates(cleanQuery);
    const isDistrictQuery = districtTitles.length > 0;
    const matchedLocalAsset = isDistrictQuery ? null : WEATHER_CITY_VISUAL_CONFIG.localAssets.find(item => item.pattern.test(cleanQuery));
    const candidates = []
        .concat(districtTitles)
        .concat(matchedLocalAsset?.wikiTitles || [])
        .concat(matchedLocalAsset?.title || [])
        .concat(cleanQuery || []);
    if (!isDistrictQuery && /^[\u4e00-\u9fa5]{2,8}$/.test(cleanQuery) && !/[市区县州盟旗]$/.test(cleanQuery)) {
        candidates.push(`${cleanQuery}市`);
    }
    return Array.from(new Set(candidates.map(item => String(item || '').trim()).filter(Boolean)));
}

// 判断百科返回页是否适合作为城市背景来源；只接受城市/行政区/景观类页面，排除人物、影视、学校、媒体等偏题图片。
function isWikimediaCityVisualPage(page, query, titleCandidates = []) {
    const imageUrl = normalizeWeatherImageUrl(page?.thumbnail?.source);
    if (!imageUrl) return false;

    const pageTitle = String(page?.title || '').trim();
    const description = String(page?.description || '').trim();
    const cleanQuery = cleanWeatherCityAssetCandidate(query).replace(/\s+/g, '');
    const combined = `${pageTitle} ${description}`;
    const titleMatched = titleCandidates.some(title => pageTitle === title || pageTitle.includes(title));
    const districtTitleMatched = titleMatched && isWeatherDistrictLevelQuery(pageTitle);
    const queryMatched = cleanQuery && combined.includes(cleanQuery);
    const citySignals = /(城市|直辖市|地级市|县级市|首都|省会|行政区|自治州|地区|市辖区|中华人民共和国|中國|中国|city|municipality|prefecture)/i;
    const skylineSignals = /(天际线|地标|城区|市中心|CBD|摩天|大厦|广场|街景|景观|建筑|skyline|downtown|landmark|cityscape)/i;
    const offTopicSignals = /(大学|学院|中学|小学|学校|酒店|饭店|宾馆|地铁|铁路|车站|机场|航空|电视台|广播|媒体|报社|报纸|杂志|电影|电视剧|动画|歌曲|专辑|小说|漫画|节目|游戏|应用程序|人物|演员|歌手|主持人|运动员|事件|事故|天气|预报|新闻联播|模板|分类)/i;

    if (offTopicSignals.test(combined)) return false;
    return districtTitleMatched || (titleMatched && (citySignals.test(combined) || queryMatched)) || (queryMatched && skylineSignals.test(combined));
}

// 将百科页面转换为天气城市图片资产；调用前必须先完成页面类型过滤。
function buildWikimediaWeatherAsset(page, query, provider = {}) {
    return {
        url: normalizeWeatherImageUrl(page.thumbnail.source),
        label: `${provider.label || WEATHER_CITY_VISUAL_CONFIG.providerLabel} · ${page.title || query}`,
        title: page.title || query,
        provider: provider.id || WEATHER_CITY_VISUAL_CONFIG.provider
    };
}

// 直接读取指定百科条目的 pageimages 缩略图；这是已知城市的首选路径，比全文搜索更稳定。
async function fetchWikimediaCityVisualByTitles(titles, query, provider = {}) {
    const safeTitles = Array.from(new Set((titles || []).map(item => String(item || '').trim()).filter(Boolean)));
    if (safeTitles.length === 0) return null;

    const params = new URLSearchParams({
        action: 'query',
        titles: safeTitles.join('|'),
        redirects: '1',
        prop: 'pageimages|description',
        pithumbsize: '1200',
        format: 'json',
        origin: '*'
    });
    const searchUrl = provider.searchUrl || WEATHER_CITY_VISUAL_CONFIG.providerSearchUrl;
    const timeoutMs = provider.timeoutMs || WEATHER_CITY_VISUAL_CONFIG.providerTimeoutMs;
    const response = await fetchWeatherAssetWithTimeout(`${searchUrl}?${params.toString()}`, timeoutMs);
    if (!response.ok) {
        throw new Error(`city asset provider returned ${response.status}`);
    }
    const payload = await response.json();
    const pages = Object.values(payload?.query?.pages || {})
        .sort((a, b) => {
            const aIndex = safeTitles.indexOf(a?.title);
            const bIndex = safeTitles.indexOf(b?.title);
            return (aIndex >= 0 ? aIndex : 999) - (bIndex >= 0 ? bIndex : 999);
        });
    const page = pages.find(item => isWikimediaCityVisualPage(item, query, safeTitles));
    return page ? buildWikimediaWeatherAsset(page, query, provider) : null;
}

// 读取开放百科 Action API 的 pageimages 缩略图，作为优先的真实城市照片来源。
async function fetchWikimediaCityVisual(query, provider = {}) {
    const titleCandidates = buildWikimediaCityTitleCandidates(query);
    const titleAsset = await fetchWikimediaCityVisualByTitles(titleCandidates, query, provider);
    if (titleAsset) return titleAsset;

    const cleanQuery = cleanWeatherCityAssetCandidate(query).replace(/\s+/g, '') || query;
    const params = new URLSearchParams({
        action: 'query',
        generator: 'search',
        gsrsearch: cleanQuery,
        gsrlimit: '5',
        prop: 'pageimages|description',
        pithumbsize: '1200',
        format: 'json',
        origin: '*'
    });
    const searchUrl = provider.searchUrl || WEATHER_CITY_VISUAL_CONFIG.providerSearchUrl;
    const timeoutMs = provider.timeoutMs || WEATHER_CITY_VISUAL_CONFIG.providerTimeoutMs;
    const url = `${searchUrl}?${params.toString()}`;
    const response = await fetchWeatherAssetWithTimeout(url, timeoutMs);
    if (!response.ok) {
        throw new Error(`city asset provider returned ${response.status}`);
    }
    const payload = await response.json();
    const pages = Object.values(payload?.query?.pages || {})
        .sort((a, b) => Number(a?.index || 999) - Number(b?.index || 999));
    const page = pages.find(item => isWikimediaCityVisualPage(item, cleanQuery, titleCandidates));
    return page ? buildWikimediaWeatherAsset(page, cleanQuery, provider) : null;
}

// 根据 provider 类型调用对应的远程素材服务；后续接高德/百度/腾讯后端代理时只需要扩展这里。
async function fetchWeatherCityVisualFromProvider(provider, query) {
    if (!provider || provider.type === 'wikimedia') {
        return fetchWikimediaCityVisual(query, provider || {});
    }
    return null;
}

// 为天气视觉区查询真实城市照片；结果会缓存，失败时显式返回空，让本地图册兜底。
async function resolveRemoteWeatherCityAsset(query) {
    const cacheKey = String(query || '').trim().toLowerCase();
    if (!cacheKey) return null;
    if (weatherCityVisualCache.has(cacheKey)) return weatherCityVisualCache.get(cacheKey);
    const isDistrictQuery = isWeatherDistrictLevelQuery(query);

    const providers = Array.isArray(WEATHER_CITY_VISUAL_CONFIG.remoteProviders) && WEATHER_CITY_VISUAL_CONFIG.remoteProviders.length > 0
        ? WEATHER_CITY_VISUAL_CONFIG.remoteProviders
        : [{ id: WEATHER_CITY_VISUAL_CONFIG.provider, type: 'wikimedia', label: WEATHER_CITY_VISUAL_CONFIG.providerLabel, searchUrl: WEATHER_CITY_VISUAL_CONFIG.providerSearchUrl }];
    const promise = (async () => {
        for (const provider of providers) {
            try {
                const providerForQuery = isDistrictQuery
                    ? {
                        ...provider,
                        timeoutMs: Math.max(provider?.timeoutMs || 0, WEATHER_CITY_VISUAL_CONFIG.districtProviderTimeoutMs || WEATHER_CITY_VISUAL_CONFIG.providerTimeoutMs)
                    }
                    : provider;
                const asset = await fetchWeatherCityVisualFromProvider(providerForQuery, query);
                if (!asset?.url) continue;
                const reachable = await verifyWeatherImageReachable(
                    asset.url,
                    isDistrictQuery ? WEATHER_CITY_VISUAL_CONFIG.districtImageTimeoutMs : WEATHER_CITY_VISUAL_CONFIG.imageTimeoutMs
                );
                if (reachable || isDistrictQuery) return asset;
            } catch (error) {
                console.warn('[Dingdang] 城市背景素材 provider 不可用，尝试下一备用来源:', provider?.id || provider?.type || 'unknown', error);
            }
        }
        return null;
    })();
    weatherCityVisualCache.set(cacheKey, promise);
    const asset = await promise;
    weatherCityVisualCache.set(cacheKey, asset);
    return asset;
}

// 按配置解析天气城市背景。有精选本地真实图册时先用本地图册，避免网络不可达或远程图库误匹配。
function resolvePreferredWeatherCityAsset(query, candidateUrls, localMeta) {
    const localResolver = () => resolveReachableWeatherCityAsset(candidateUrls, localMeta);
    const hasLocalPhotoAlbum = hasWeatherLandmarkPhotoCandidate(candidateUrls);
    if (WEATHER_CITY_VISUAL_CONFIG.preferLocalPhotoAlbum === true && hasLocalPhotoAlbum) {
        return localResolver().then(localAsset => localAsset || resolveRemoteWeatherCityAsset(query));
    }
    if (WEATHER_CITY_VISUAL_CONFIG.preferRemotePhoto !== false && query) {
        return resolveRemoteWeatherCityAsset(query).then(remoteAsset => remoteAsset || localResolver());
    }
    return localResolver().then(localAsset => localAsset || resolveRemoteWeatherCityAsset(query));
}

// 为一次城市图片刷新生成稳定 key，防止旧异步请求把上一轮城市图回写到新卡片。
function buildWeatherCityAssetRequestKey(query, candidateUrls) {
    return `${String(query || '').trim()}::${(candidateUrls || []).join('|')}`;
}

// 在 A2UI 渲染后把远程城市素材写回天气视觉区，避免等待网络素材时阻塞天气卡内容。
function hydrateWeatherCityVisuals(root = document) {
    const stages = Array.from(root.querySelectorAll('.weather-visual-stage[data-city-asset-state]:not([data-city-asset-state="loaded"]):not([data-city-asset-state="fallback"]):not([data-city-asset-state="failed"])'));
    stages.forEach(stage => {
        const query = stage.getAttribute('data-city-asset-query') || '';
        const candidateUrls = decodeWeatherAssetUrls(stage.getAttribute('data-city-asset-urls') || '');
        if (!query && candidateUrls.length === 0) return;
        const requestKey = buildWeatherCityAssetRequestKey(query, candidateUrls);
        stage.setAttribute('data-city-asset-request', requestKey);
        stage.setAttribute('data-city-asset-state', 'loading');
        stage.classList.add('is-city-image-refreshing');
        resolvePreferredWeatherCityAsset(query, candidateUrls, {
            label: stage.getAttribute('data-city-asset-label') || '',
            title: stage.getAttribute('data-city-asset-title') || query,
            provider: 'local-album'
        }).then(asset => {
            if (!stage.isConnected || stage.getAttribute('data-city-asset-request') !== requestKey) return;
            stage.classList.remove('is-city-image-refreshing');
            if (!asset?.url) {
                stage.setAttribute('data-city-asset-state', 'fallback');
                stage.setAttribute('data-city-asset-provider', 'local-css');
                return;
            }
            stage.style.setProperty('--weather-city-image', `url("${asset.url.replace(/"/g, '%22')}")`);
            stage.classList.remove('is-generic-city');
            stage.classList.add('has-city-image');
            stage.setAttribute('data-city-asset-state', 'loaded');
            stage.setAttribute('data-city-asset-provider', asset.provider || WEATHER_CITY_VISUAL_CONFIG.provider);
            stage.setAttribute('data-city-asset-title', asset.title || query);
            stage.classList.add('weather-city-image-redrawn');
            window.setTimeout(() => stage.classList.remove('weather-city-image-redrawn'), 720);
        }).catch(error => {
            if (!stage.isConnected || stage.getAttribute('data-city-asset-request') !== requestKey) return;
            stage.classList.remove('is-city-image-refreshing');
            stage.setAttribute('data-city-asset-state', 'failed');
            stage.setAttribute('data-city-asset-provider', 'local-css');
            console.warn('[Dingdang] 城市背景素材刷新失败:', error);
        });
    });
}

// 汇总天气状态文本，供天气符号和背景氛围判断使用。
function getWeatherConditionText(weather) {
    return [
        weather.condition,
        weather.summary,
        weather.title,
        weather.precipitation,
        weather.wind
    ].filter(Boolean).join(' ');
}

// 将天气描述映射成直观符号，避免仅用文字表达天气状态。
function getWeatherSymbol(weather) {
    if (weather.loading) return '◌';
    if (weather.needsInput) return '⌖';
    const text = getWeatherConditionText(weather);
    if (/雷|暴雨|thunder|storm/i.test(text)) return '⛈';
    if (/雪|冰雹|sleet|snow|hail/i.test(text)) return '❄';
    if (/雨|阵雨|小雨|中雨|大雨|降水|rain|shower/i.test(text)) return '☔';
    if (/雾|霾|烟|fog|haze|mist/i.test(text)) return '▧';
    if (/晴|sunny|clear/i.test(text)) return '☀';
    if (/多云|阴|云|cloud|overcast/i.test(text)) return '☁';
    if (/风|wind/i.test(text)) return '≋';
    return '◐';
}

// 根据天气类型给视觉区加氛围 class，控制云、雨、雾、晴光等效果。
function getWeatherAtmosphereClass(weather) {
    if (weather.loading) return 'weather-atmosphere-loading';
    if (weather.needsInput) return 'weather-atmosphere-pending';
    const text = getWeatherConditionText(weather);
    if (/雷|暴雨|thunder|storm/i.test(text)) return 'weather-atmosphere-storm';
    if (/雪|冰雹|sleet|snow|hail/i.test(text)) return 'weather-atmosphere-snow';
    if (/雨|阵雨|小雨|中雨|大雨|降水|rain|shower/i.test(text)) return 'weather-atmosphere-rain';
    if (/雾|霾|烟|fog|haze|mist/i.test(text)) return 'weather-atmosphere-fog';
    if (/晴|sunny|clear/i.test(text)) return 'weather-atmosphere-sunny';
    if (/多云|阴|云|cloud|overcast/i.test(text)) return 'weather-atmosphere-cloudy';
    return 'weather-atmosphere-default';
}

// 渲染天气顶部视觉区：城市背景、天气符号、温度和关键状态共处；素材来源只做内部状态，不进入用户主界面。
function renderWeatherVisualStage(weather, subtitle) {
    const localAsset = resolveLocalWeatherCityAsset(weather);
    const assetQuery = normalizeWeatherCityAssetQuery(weather);
    const localAssetUrls = normalizeWeatherAssetUrlList(localAsset);
    const shouldUseLocalPlaceholder = WEATHER_CITY_VISUAL_CONFIG.preferRemotePhoto === false
        || WEATHER_CITY_VISUAL_CONFIG.useLocalPlaceholder === true
        || !assetQuery;
    const initialAssetUrl = shouldUseLocalPlaceholder ? (localAssetUrls[0] || '') : '';
    const hasCityImage = Boolean(initialAssetUrl);
    const style = hasCityImage
        ? ` style="--weather-city-image: url('${escapeHTML(initialAssetUrl).replace(/'/g, '%27')}')"`
        : '';
    const assetAttrs = [
        `data-city-asset-state="${hasCityImage ? 'candidate' : assetQuery ? 'idle' : 'fallback'}"`,
        assetQuery ? `data-city-asset-query="${escapeHTMLAttribute(assetQuery)}"` : '',
        localAssetUrls.length > 0 ? `data-city-asset-urls="${escapeHTMLAttribute(encodeWeatherAssetUrls(localAssetUrls))}"` : '',
        localAsset?.label ? `data-city-asset-label="${escapeHTMLAttribute(localAsset.label)}"` : '',
        localAsset?.title ? `data-city-asset-title="${escapeHTMLAttribute(localAsset.title)}"` : ''
    ].filter(Boolean).join(' ');
    const condition = weather.condition || (weather.loading ? '正在查询' : weather.needsInput ? '等待位置' : '实时天气');
    const symbol = getWeatherSymbol(weather);
    const atmosphereClass = getWeatherAtmosphereClass(weather);
    const imageLabel = localAsset?.label || (assetQuery ? `${assetQuery}城市天气背景` : `${weather.city || '城市'}通用气象光影`);

    return `
        <div class="weather-visual-stage ${atmosphereClass} ${hasCityImage ? 'has-city-image' : 'is-generic-city'}"${style} ${assetAttrs}>
            <div class="weather-visual-image" aria-label="${escapeHTMLAttribute(imageLabel)}"></div>
            <div class="weather-visual-scan"></div>
            <div class="weather-visual-cloud weather-visual-cloud-a"></div>
            <div class="weather-visual-cloud weather-visual-cloud-b"></div>
            <div class="weather-visual-rain"></div>
            <div class="weather-visual-header">
                <div>
                    <div class="weather-card-title">${escapeHTML(weather.title || '天气查询')}</div>
                    <div class="weather-card-subtitle">${escapeHTML(subtitle)}</div>
                </div>
                <div class="weather-symbol-orb" role="img" aria-label="${escapeHTMLAttribute(condition)}">
                    <span>${escapeHTML(symbol)}</span>
                </div>
            </div>
            <div class="weather-visual-main">
                <div>
                    <div class="weather-card-temp">${escapeHTML(formatWeatherHeroTemperature(weather))}</div>
                    <div class="weather-visual-condition">${escapeHTML(condition)}</div>
                </div>
            </div>
        </div>
    `;
}

// 渲染天气结果的数据源和 ACPs 返回通道，让“数据从哪里来”和“协议怎么回传”分开可见。
function renderWeatherSourcePanel(weather) {
    const rows = [
        ['天气数据', formatWeatherDataSourceLabel(weather)],
        ['调用通道', weather.transportSource || 'ACPs/AIP']
    ];

    return `
        <div class="weather-source-panel">
            ${rows.map(([label, value]) => `
                <div class="weather-source-item">
                    <span>${escapeHTML(label)}</span>
                    <strong>${renderInlineMarkdown(value)}</strong>
                </div>
            `).join('')}
        </div>
    `;
}

// 渲染天气专用卡片；主内容只使用结构化字段，不再把天气 Markdown 直接铺出来。
function renderWeatherA2uiCard(weather) {
    const metrics = [
        ['温度', weather.temperature, 'weather-metric-temp'],
        ['天气', weather.condition, 'weather-metric-condition'],
        ['降水', weather.precipitation, 'weather-metric-rain'],
        ['湿度', weather.humidity, 'weather-metric-humidity'],
        ['风况', weather.wind, 'weather-metric-wind']
    ].filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== '');

    const titleParts = [weather.city, weather.date].filter(Boolean).join(' · ');
    const subtitle = titleParts || weather.query || '天气查询';
    const chartHtml = weather.chart ? `<div class="weather-chart-wrap">${renderAgentChart(weather.chart)}</div>` : '';
    const forecastRows = Array.isArray(weather.forecast) ? weather.forecast : [];
    const forecastHtml = forecastRows.length > 0 ? `
        <div class="weather-forecast-panel">
            <div class="weather-forecast-head">
                <span>未来天气</span>
                <small>Agent 返回的预报数据</small>
            </div>
            <div class="weather-forecast-list">
                ${forecastRows.map(row => `
                    <div class="weather-forecast-row">
                        <strong>${escapeHTML(row.date)}</strong>
                        <span>${escapeHTML(row.condition || '--')}</span>
                        <em>${escapeHTML(row.temperature || row.wind || '--')}</em>
                    </div>
                `).join('')}
            </div>
        </div>
    ` : '';
    const detailHtml = metrics.length === 0 && Array.isArray(weather.details) && weather.details.length > 0 ? `
        <div class="weather-detail-panel">
            <span>天气说明</span>
            <ul class="weather-detail-list">
                ${weather.details.map(line => `<li>${renderInlineMarkdown(line)}</li>`).join('')}
            </ul>
        </div>
    ` : '';
    const loadingBar = weather.loading ? '<div class="weather-live-bar"><span></span></div>' : '';
    let inputPanel = '';
    if (weather.needsInput && !weather.useAppLevelInput) {
        const inputId = `${weather.cardRef || 'weather-card'}-location-input`;
        const weatherKeydownAction = renderDynamicActionAttributes('weather-input-keydown', { cardRef: String(weather.cardRef || '') });
        const weatherSubmitAction = renderDynamicActionAttributes('submit-weather-input', { cardRef: String(weather.cardRef || '') });
        const weatherLocationAction = renderDynamicActionAttributes('use-weather-location', { cardRef: String(weather.cardRef || '') });
        inputPanel = `
            <div class="weather-input-panel" data-card-ref="${escapeHTMLAttribute(weather.cardRef || '')}">
                <div class="weather-input-copy">
                    <strong>需要补充位置</strong>
                    <span>${escapeHTML(weather.inputPrompt || '请输入城市，或授权使用当前位置。')}</span>
                </div>
                <div class="weather-input-row">
                    <input
                        id="${escapeHTMLAttribute(inputId)}"
                        class="weather-location-input"
                        type="text"
                        placeholder="例如：广州"
                        ${weatherKeydownAction}
                    >
                    <button class="weather-input-submit" type="button" ${weatherSubmitAction}>继续</button>
                </div>
                <button class="weather-use-location" type="button" ${weatherLocationAction} ${weather.canUseLocation ? '' : 'disabled'}>
                    使用当前位置
                </button>
            </div>
        `;
    }

    return `
        <section class="weather-card ${weather.loading ? 'is-loading' : ''} ${weather.needsInput ? 'needs-input' : ''}">
            <div class="weather-card-topline">
                <span class="weather-card-badge">${weather.needsInput ? '等待输入' : '实时天气'}</span>
                <span class="weather-card-source">${escapeHTML(getWeatherToplineSourceLabel(weather))}</span>
            </div>
            ${renderWeatherVisualStage(weather, subtitle)}
            ${loadingBar}
            <div class="weather-card-summary">${renderInlineMarkdown(weather.summary || '暂无天气摘要')}</div>
            ${renderWeatherSourcePanel(weather)}
            ${inputPanel}
            <div class="weather-metric-grid">
                ${metrics.map(([label, value, className]) => `
                    <div class="weather-metric ${className}">
                        <span>${escapeHTML(label)}</span>
                        <strong>${escapeHTML(String(value))}</strong>
                    </div>
                `).join('')}
            </div>
            ${forecastHtml}
            ${detailHtml}
            ${chartHtml}
            <div class="weather-card-foot">
                ${weather.updatedAt ? `<span>更新：${escapeHTML(weather.updatedAt)}</span>` : '<span>通过 ACPs/AIP 返回</span>'}
                ${weather.query ? `<span>任务：${escapeHTML(weather.query)}</span>` : ''}
            </div>
        </section>
    `;
}

// 当任务是天气查询时，把 AIP Markdown 产物改写为受控天气 A2UI 模板。
function buildWeatherBackedA2uiMessages(event, markdown, cardDomId) {
    const surfaceId = `${cardDomId}-surface`;
    return [
        { type: 'createSurface', surfaceId, root: 'root' },
        {
            type: 'updateDataModel',
            surfaceId,
            path: '/',
            value: {
                weather: buildWeatherCardData(event, markdown || '')
            }
        },
        {
            type: 'updateComponents',
            surfaceId,
            components: [
                { id: 'root', component: 'Card', child: 'weather' },
                { id: 'weather', component: 'WeatherCard', data: { path: '/weather' } }
            ]
        }
    ];
}

// 渲染 Markdown 与 json-chart 混合内容，非法图表保留为代码块方便排查。
function renderAgentCardContent(markdown, jsonSummaries = [], dataItems = []) {
    if (Array.isArray(jsonSummaries) && jsonSummaries.length) {
        return renderChatJsonSummaries(markdown, jsonSummaries, { dataItems });
    }
    const parts = parseAgentCardParts(markdown);
    let dataItemTarget = -1;
    for (let index = parts.length - 1; index >= 0; index -= 1) {
        if (parts[index].type === 'markdown') {
            dataItemTarget = index;
            break;
        }
    }
    const rendered = parts.map((part, index) => {
        if (part.type === 'chart') {
            const chart = parseJsonChart(part.content);
            if (chart) return renderAgentChart(chart);
            return `<pre class="agent-chart-fallback"><code>${escapeHTML(part.content)}</code></pre>`;
        }
        const partDataItems = index === dataItemTarget ? dataItems : [];
        const jsonTreeHtml = window.DingdangJsonTreeRenderer?.tryRenderJsonTree(part.content) || '';
        const contentHtml = jsonTreeHtml
            ? jsonTreeHtml + renderUnreferencedDataItemResources(part.content, partDataItems)
            : renderRichMessage(part.content, { dataItems: partDataItems });
        return `<div class="agent-result-markdown">${contentHtml}</div>`;
    }).join('');
    return dataItemTarget === -1
        ? rendered + renderUnreferencedDataItemResources('', dataItems)
        : rendered;
}

// 生成稳定的 Agent 结果卡片 DOM ID；同一个 card_id/task_id 会原地更新。
function getAgentResultCardDomId(event) {
    const source = event.card_id || event.cardId || event.task_id || `${event.conversation_id || 'conv'}-${event.agent_name || event.agent || 'agent'}`;
    const safe = String(source)
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, '-')
        .replace(/-+/g, '-')
        .replace(/^-|-$/g, '') || `agent-card-${Date.now()}`;
    return `agent-result-${safe}`;
}

// 生成关闭实时渲染时使用的普通消息 DOM ID，确保同一个 card_id 的 working/final 结果原地更新。
function getRawAgentResultMessageDomId(event) {
    return getAgentResultCardDomId(event).replace(/^agent-result-/, 'agent-raw-result-');
}

// 判断关闭实时渲染后是否应该把当前 AIP 事件显示成普通文本；working 空事件只保留在流程卡里。
function shouldRenderRawAgentResult(event, content = '') {
    return shouldRenderInlineAgentResult(event, content || extractAgentCardContent(event));
}

// 将 ACPs/AIP 返回内容按普通 Markdown 消息呈现，不进入 A2UI、结果卡片、原始模式或全屏应用。
function addStandardAgentResultMessage(event, options = {}) {
    const content = options.content || extractAgentCardContent(event);
    if (!options.force && !shouldRenderRawAgentResult(event, content || '')) {
        setInlineExecutionStatus(
            event.message || `${event.agent_name || event.agent || '智能体'} 正在执行`,
            'running'
        );
        return false;
    }
    if (elements.messagesContainer.querySelector('.welcome-message')) {
        clearDynamicActionElement(elements.messagesContainer);
    }
    state.hasAgentResultCardsInCurrentRun = true;
    state.hasRawAgentResultsInCurrentRun = false;

    const renderEvent = content ? { ...event, content } : event;
    const messageDomId = getAgentResultCardDomId(event);
    const stateLabel = formatAgentCardState(event.state || event.status);
    const stateClass = getAgentCardStateClass(event.state || event.status);
    const agentName = event.agent_name || event.agent || '智能体';
    const body = content || event.message || '智能体已返回结果。';
    rememberAgentResultEvent(renderEvent);
    removeRawAgentResultMessage(event);
    const html = `
        <div class="message assistant agent-result-message" id="${escapeHTMLAttribute(messageDomId)}" data-card-id="${escapeHTMLAttribute(event.card_id || event.cardId || '')}" data-agent-state="${escapeHTMLAttribute(stateClass)}">
            <div class="message-avatar">
                <img src="dingdang.png" alt="叮当">
            </div>
            <div class="message-content-wrapper">
                <div class="agent-result-message-meta">
                    <strong>${escapeHTML(agentName)}</strong>
                    <span>${escapeHTML(stateLabel || '已返回')}</span>
                </div>
                <div class="message-content">${renderRichMessage(body, { dataItems: getAgentResultDataItems(renderEvent) })}</div>
                ${renderAgentOutputArtifacts(renderEvent)}
                ${renderCopyButton(body)}
            </div>
        </div>
    `;

    const existingMessage = document.getElementById(messageDomId);
    if (existingMessage) {
        const template = document.createElement('template');
        template.innerHTML = html.trim();
        const nextMessage = template.content.firstElementChild;
        if (nextMessage) {
            nextMessage.classList.add('agent-result-message-updated');
            releaseDynamicActionPayloads(existingMessage);
            existingMessage.replaceWith(nextMessage);
        } else {
            releaseDynamicActionPayloads(existingMessage);
            existingMessage.outerHTML = html;
        }
    } else {
        elements.messagesContainer.insertAdjacentHTML('beforeend', html);
    }
    hydrateAgentArtifactMedia(document.getElementById(messageDomId));
    scrollToBottom();
    return true;
}

// 兼容历史内部调用名；浏览器仅呈现统一 Markdown 消息。
function addRawAgentResultMessage(event, options = {}) {
    return addStandardAgentResultMessage(event, options);
}

// 删除某个 Agent 事件对应的 A2UI 聊天卡片；切到原始返回模式时避免两种视图并存。
function removeRenderedAgentResultCard(event) {
    const card = document.getElementById(getAgentResultCardDomId(event));
    removeDynamicActionElement(card);
}

// 删除某个 Agent 事件对应的原始返回消息；恢复实时渲染时使用，保留内存中的 AIP 事件缓存。
function removeRawAgentResultMessage(event) {
    const messageDomId = getRawAgentResultMessageDomId(event);
    const message = document.getElementById(messageDomId);
    removeDynamicActionElement(message);
    delete state.rawAgentResultCopyTexts[messageDomId];
}

// 尽量从字符串或对象中归一化 A2UI 消息数组，兼容事件字段、DataItem 和 fenced block。
function normalizeA2uiMessageList(value) {
    if (!value) return [];
    if (typeof value === 'string') {
        try {
            return normalizeA2uiMessageList(JSON.parse(stripJsonComments(value)));
        } catch (error) {
            console.warn('Invalid A2UI JSON:', error);
            return [];
        }
    }
    if (Array.isArray(value)) return value.filter(Boolean);
    if (Array.isArray(value.messages)) return value.messages.filter(Boolean);
    if (Array.isArray(value.a2ui_messages)) return value.a2ui_messages.filter(Boolean);
    if (Array.isArray(value.a2uiMessages)) return value.a2uiMessages.filter(Boolean);
    if (typeof value === 'object') return [value];
    return [];
}

// 从 Markdown fenced block 中提取 A2UI JSON，便于 Wit 在 Product.text 中直接携带 UI 更新。
function extractA2uiMessagesFromMarkdown(markdown) {
    const messages = [];
    const fenceRegex = /```(a2ui|a2ui-json)\s*\n([\s\S]*?)```/gi;
    let match;
    while ((match = fenceRegex.exec(markdown || '')) !== null) {
        messages.push(...normalizeA2uiMessageList(match[2] || ''));
    }
    return messages;
}

// 从 DataItem 中提取 A2UI 消息，允许后续 Wit 返回结构化 A2UI DataItem。
function extractA2uiMessagesFromProducts(products) {
    const messages = [];
    (Array.isArray(products) ? products : []).forEach(product => {
        const items = Array.isArray(product?.dataItems) ? product.dataItems : [];
        items.forEach(item => {
            const format = String(item?.format || item?.mimeType || item?.metadata?.format || item?.metadata?.ui?.format || '').toLowerCase();
            if (item?.type === 'a2ui' || format.includes('a2ui')) {
                messages.push(...normalizeA2uiMessageList(item.data || item.text));
            }
        });
    });
    return messages;
}

// 读取或初始化某张可更新卡片的 A2UI surface 状态。
function getA2uiSurfaceStore(cardDomId) {
    if (!state.agentResultCards[cardDomId]) {
        state.agentResultCards[cardDomId] = {
            surfaceId: `${cardDomId}-surface`,
            rootId: 'root',
            components: {},
            dataModel: {},
            eventDataItems: [],
            renderedEventDataItems: false,
            deleted: false
        };
    }
    return state.agentResultCards[cardDomId];
}

// 使用 JSON Pointer 风格路径读取 data model，支撑 A2UI DataBinding。
function getA2uiDataByPath(dataModel, path) {
    if (!path || path === '/') return dataModel;
    const segments = String(path).replace(/^\//, '').split('/').filter(Boolean);
    return segments.reduce((value, segment) => {
        if (value && Object.prototype.hasOwnProperty.call(value, segment)) {
            return value[segment];
        }
        return undefined;
    }, dataModel);
}

// 使用 JSON Pointer 风格路径写入 data model，用于 updateDataModel 增量更新。
function setA2uiDataByPath(dataModel, path, value) {
    if (!path || path === '/') {
        Object.keys(dataModel).forEach(key => delete dataModel[key]);
        if (value && typeof value === 'object' && !Array.isArray(value)) {
            Object.assign(dataModel, value);
        } else {
            dataModel.value = value;
        }
        return;
    }

    const segments = String(path).replace(/^\//, '').split('/').filter(Boolean);
    let cursor = dataModel;
    segments.slice(0, -1).forEach(segment => {
        if (!cursor[segment] || typeof cursor[segment] !== 'object') cursor[segment] = {};
        cursor = cursor[segment];
    });
    cursor[segments[segments.length - 1]] = value;
}

// 解析 A2UI 绑定值，支持 v0.8 literalString 与 v0.9 DataBinding path。
function resolveA2uiValue(value, store) {
    if (value && typeof value === 'object') {
        if (typeof value.literalString === 'string') return value.literalString;
        if (typeof value.path === 'string') {
            const resolved = getA2uiDataByPath(store.dataModel, value.path);
            return resolved === undefined || resolved === null ? '' : resolved;
        }
    }
    return value === undefined || value === null ? '' : value;
}

// 把 v0.8 wrapper 和 v0.9 扁平 component 写法归一成统一渲染结构。
function normalizeA2uiComponent(componentDef) {
    if (!componentDef || typeof componentDef !== 'object') return { type: '', props: {} };
    if (typeof componentDef.component === 'string') {
        return { type: componentDef.component, props: componentDef };
    }
    if (componentDef.component && typeof componentDef.component === 'object') {
        const type = Object.keys(componentDef.component)[0];
        return { type, props: componentDef.component[type] || {} };
    }
    return { type: componentDef.type || '', props: componentDef };
}

// 提取 A2UI children 字段，兼容 v0.8 explicitList 和 v0.9 children 数组。
function getA2uiChildren(props) {
    const children = props.children;
    if (Array.isArray(children)) return children;
    if (Array.isArray(children?.explicitList)) return children.explicitList;
    if (typeof props.child === 'string') return [props.child];
    return [];
}

// 限制图片 URL 协议，避免 A2UI/Markdown 内容注入危险链接。
function getSafeResourceUrl(url) {
    try {
        const parsed = new URL(String(url || ''), window.location.origin);
        if (['http:', 'https:', 'data:'].includes(parsed.protocol)) return parsed.href;
    } catch {}
    return '';
}

// 渲染单个 A2UI 组件。MC 是本地自定义组件，用于承载 Markdown/json-chart 素材。
function renderA2uiComponent(componentId, store, depth = 0) {
    if (depth > 30) return '';
    const componentDef = typeof componentId === 'object' ? componentId : store.components[componentId];
    if (!componentDef) return '';

    const { type, props } = normalizeA2uiComponent(componentDef);
    const normalizedType = String(type || '').toLowerCase();
    const childrenHtml = getA2uiChildren(props)
        .map(childId => renderA2uiComponent(childId, store, depth + 1))
        .join('');

    if (normalizedType === 'card') {
        return `<div class="a2ui-mc-card">${childrenHtml}</div>`;
    }
    if (normalizedType === 'column' || normalizedType === 'list') {
        return `<div class="a2ui-mc-column">${childrenHtml}</div>`;
    }
    if (normalizedType === 'row') {
        return `<div class="a2ui-mc-row">${childrenHtml}</div>`;
    }
    if (normalizedType === 'divider') {
        return '<hr class="a2ui-mc-divider">';
    }
    if (normalizedType === 'text') {
        const variant = String(props.variant || props.usageHint || 'body').toLowerCase();
        const text = resolveA2uiValue(props.text ?? props.value, store);
        return `<div class="a2ui-mc-text a2ui-mc-text-${escapeHTML(variant)}">${escapeHTML(String(text))}</div>`;
    }
    if (normalizedType === 'markdown' || normalizedType === 'markdowncontent' || normalizedType === 'mc') {
        const markdown = resolveA2uiValue(props.markdown ?? props.content ?? props.text ?? props.value, store);
        const jsonSummaries = resolveA2uiValue(props.jsonSummaries, store);
        const boundDataItems = resolveA2uiValue(props.dataItems, store);
        const dataItems = Array.isArray(boundDataItems)
            ? boundDataItems
            : (!store.renderedEventDataItems ? store.eventDataItems : []);
        if (dataItems.length > 0) store.renderedEventDataItems = true;
        return `<div class="a2ui-mc-markdown">${renderAgentCardContent(String(markdown || ''), jsonSummaries, dataItems)}</div>`;
    }
    if (normalizedType === 'image') {
        const url = getSafeResourceUrl(resolveA2uiValue(props.url, store));
        if (!url) return '';
        return `<img class="a2ui-mc-image" src="${escapeHTMLAttribute(url)}" alt="${escapeHTMLAttribute(props.alt || props.accessibility?.label || '')}">`;
    }
    if (normalizedType === 'button') {
        const label = resolveA2uiValue(props.label || props.text || props.title, store) || '操作';
        return `<button class="a2ui-mc-button" type="button" disabled>${escapeHTML(String(label))}</button>`;
    }
    if (normalizedType === 'weathercard' || normalizedType === 'weather') {
        const weather = resolveA2uiValue(props.data ?? props.weather ?? props.value, store);
        return renderWeatherA2uiCard(weather && typeof weather === 'object' ? weather : {});
    }

    return childrenHtml || '';
}

// 应用一条 A2UI lifecycle 消息，支持 createSurface/updateComponents/updateDataModel/deleteSurface。
function applyA2uiMessage(store, message) {
    const messageType = String(message?.type || message?.messageType || message?.kind || '').toLowerCase();
    if (!messageType) return;

    if (messageType === 'createsurface') {
        store.surfaceId = message.surfaceId || store.surfaceId;
        store.rootId = message.root || message.rootId || message.rootComponentId || message.componentId || store.rootId || 'root';
        store.deleted = false;
        return;
    }

    if (messageType === 'updatecomponents') {
        store.surfaceId = message.surfaceId || store.surfaceId;
        (message.components || []).forEach(component => {
            if (component?.id) store.components[component.id] = component;
        });
        if (message.root || message.rootId || message.rootComponentId) {
            store.rootId = message.root || message.rootId || message.rootComponentId;
        }
        return;
    }

    if (messageType === 'updatedatamodel') {
        store.surfaceId = message.surfaceId || store.surfaceId;
        setA2uiDataByPath(store.dataModel, message.path || '/', message.value ?? message.data);
        return;
    }

    if (messageType === 'deletesurface') {
        store.deleted = true;
    }
}

// 当 Wit 只返回 Markdown 时，把 Markdown 包装成 A2UI Surface，并用本地 MC 组件渲染。
function buildMarkdownBackedA2uiMessages(event, markdown, cardDomId) {
    const surfaceId = `${cardDomId}-surface`;
    return [
        { type: 'createSurface', surfaceId, root: 'root' },
        {
            type: 'updateDataModel',
            surfaceId,
            path: '/',
            value: {
                title: event.agent_name || event.agent || 'Agent Result',
                markdown: markdown || '未返回可展示内容',
                dataItems: getAgentResultDataItems(event)
            }
        },
        {
            type: 'updateComponents',
            surfaceId,
            components: [
                { id: 'root', component: 'Card', child: 'body' },
                { id: 'body', component: 'Column', children: ['title', 'content'] },
                { id: 'title', component: 'Text', text: { path: '/title' }, variant: 'caption' },
                { id: 'content', component: 'MC', markdown: { path: '/markdown' }, dataItems: { path: '/dataItems' } }
            ]
        }
    ];
}

// 为尚未做专用组件的预设生成统一应用壳，内容仍由 MC 负责渲染 Markdown 和 json-chart。
function buildPresetMarkdownA2uiMessages(event, markdown, cardDomId, preset) {
    const resolvedPreset = preset || resolveAgentAppPreset(event, markdown, { allowFallback: true });
    const surfaceId = `${cardDomId}-surface`;
    return [
        { type: 'createSurface', surfaceId, root: 'root' },
        {
            type: 'updateDataModel',
            surfaceId,
            path: '/',
            value: {
                presetTitle: resolvedPreset?.title || '智能体应用',
                presetStatus: resolvedPreset?.supportLabel || 'MC Markdown',
                jsonSummaries: event.json_summaries || [],
                markdown: markdown || '未返回可展示内容',
                dataItems: getAgentResultDataItems(event)
            }
        },
        {
            type: 'updateComponents',
            surfaceId,
            components: [
                { id: 'root', component: 'Card', child: 'body' },
                { id: 'body', component: 'Column', children: ['heading', 'content'] },
                { id: 'heading', component: 'Column', children: ['title', 'status'] },
                { id: 'title', component: 'Text', text: { path: '/presetTitle' }, variant: 'caption' },
                { id: 'status', component: 'Text', text: { path: '/presetStatus' }, variant: 'overline' },
                { id: 'content', component: 'MC', markdown: { path: '/markdown' }, jsonSummaries: { path: '/jsonSummaries' }, dataItems: { path: '/dataItems' } }
            ]
        }
    ];
}

// JSON 是独立的通用内容类型；只检查完整对象/数组，不读取 Agent 或字段信息。
function isJsonContainer(markdown) {
    return Boolean(window.DingdangJsonTreeRenderer?.isJsonContainer(markdown));
}

// 获取事件中的 A2UI 消息；无显式 A2UI 时自动按预设或 Markdown 包装成 MC surface。
function getA2uiMessagesForEvent(event, markdown, cardDomId, preset = null) {
    const explicitMessages = [
        ...normalizeA2uiMessageList(event.a2ui_messages || event.a2uiMessages || event.a2ui),
        ...extractA2uiMessagesFromProducts(event.products),
        ...extractA2uiMessagesFromMarkdown(markdown)
    ];
    if (explicitMessages.length > 0) return explicitMessages;
    const resolvedPreset = isJsonContainer(markdown)
        ? getAgentAppPresetById('json.mc.v1')
        : preset || resolveAgentAppPreset(event, markdown, { allowFallback: false });
    if (resolvedPreset?.buildMessages) {
        return resolvedPreset.buildMessages(event, markdown, cardDomId, resolvedPreset);
    }
    return buildMarkdownBackedA2uiMessages(event, markdown, cardDomId);
}

// 渲染某次 Agent 返回对应的 A2UI surface；同一卡片多次事件会复用 store 并原地更新。
function renderA2uiAgentSurface(event, markdown, cardDomId, preset = null) {
    const store = getA2uiSurfaceStore(cardDomId);
    store.eventDataItems = getAgentResultDataItems(event);
    store.renderedEventDataItems = false;
    getA2uiMessagesForEvent(event, markdown, cardDomId, preset).forEach(message => applyA2uiMessage(store, message));
    if (store.deleted) return '<div class="agent-chart-empty">卡片已被移除</div>';
    const surfaceHtml = renderA2uiComponent(store.rootId, store);
    const fallbackResources = store.renderedEventDataItems
        ? ''
        : renderAgentDataItemResources(event, '');
    return `<div class="agent-a2ui-surface" data-surface-id="${escapeHTMLAttribute(store.surfaceId)}">${surfaceHtml}${fallbackResources}</div>`;
}

// 格式化 AIP 状态标签，让 awaiting-completion 等协议状态在演示时更易读。
function formatAgentCardState(state) {
    const normalized = String(state || 'unknown').toLowerCase();
    const labels = {
        accepted: '已接受',
        working: '执行中',
        'awaiting-input': '等待输入',
        'awaiting-completion': '等待确认',
        completed: '已完成',
        failed: '失败',
        rejected: '已拒绝',
        canceled: '已取消',
        success: '成功',
        error: '失败'
    };
    return labels[normalized] || normalized;
}

// 将 AIP 状态转换为安全 CSS 类名，避免 endpoint 返回异常状态时污染 DOM class。
function getAgentCardStateClass(state) {
    return String(state || 'unknown')
        .toLowerCase()
        .replace(/[^a-z0-9_-]/g, '-')
        .replace(/-+/g, '-')
        .replace(/^-|-$/g, '') || 'unknown';
}

// 构造 ACPs 调用证据行，展示 Agent、AIC、endpoint、taskId、状态与耗时。
function renderAgentCardEvidence(event) {
    const deployment = getAgentDeploymentScope(event);
    const evidence = [
        ['来源', deployment.evidenceLabel],
        ['Agent', event.agent_name || event.agent],
        ['AIC', event.aic],
        ['Endpoint', event.endpoint],
        ['Task', event.task_id],
        ['State', event.state || event.status],
        ['Poll', event.poll_count],
        ['Time', event.elapsed_time !== undefined ? `${event.elapsed_time}s` : '']
    ].filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== '');

    return evidence.map(([label, value]) => `
        <span class="agent-evidence-item">
            <strong>${escapeHTML(label)}</strong>${escapeHTML(String(value))}
        </span>
    `).join('');
}

// 渲染原始 TaskResult 折叠区，默认收起用于现场证明 ACPs/AIP 原始返回。
function renderRawTaskResult(event) {
    const raw = event.raw_task_result || event.rawTaskResult || null;
    if (!raw) return '';
    const json = JSON.stringify(raw, null, 2);
    return `
        <details class="agent-raw-result">
            <summary>原始 AIP TaskResult</summary>
            <pre><code>${escapeHTML(json)}</code></pre>
        </details>
    `;
}

function renderAgentOutputArtifacts(event) {
    const artifacts = Array.isArray(event.output_artifacts) ? event.output_artifacts : [];
    if (artifacts.length === 0) return '';
    return `<div class="agent-output-artifacts">${artifacts.map(item => {
        const id = escapeHTMLAttribute(item.artifact_id || '');
        const name = escapeHTML(item.filename || '智能体附件');
        if (item.status !== 'ready') {
            return `<div class="agent-output-artifact is-unavailable">📎 ${name}<span>${escapeHTML(item.status || '不可用')}</span></div>`;
        }
        if (item.display_kind === 'image') {
            return `<figure class="agent-output-artifact"><img data-artifact-media="${id}" alt="${escapeHTMLAttribute(item.filename || '智能体附件')}"><figcaption>${name}</figcaption></figure>`;
        }
        if (item.display_kind === 'audio') {
            return `<div class="agent-output-artifact"><audio controls data-artifact-media="${id}"></audio><span>${name}</span></div>`;
        }
        const actionAttributes = renderDynamicActionAttributes('download-agent-artifact', {
            artifactId: String(item.artifact_id || ''),
            filename: String(item.filename || 'download')
        });
        return `<button class="agent-output-artifact" type="button" ${actionAttributes}>📄 下载 ${name}</button>`;
    }).join('')}</div>`;
}

async function fetchAgentArtifactBlob(artifactId) {
    const response = await fetch(`${API_BASE}/artifacts/${encodeURIComponent(artifactId)}/download`, {
        headers: { ...getAuthHeaders() }
    });
    if (!response.ok) throw await createHttpError(response, '读取智能体附件');
    return await response.blob();
}

async function hydrateAgentArtifactMedia(root) {
    if (!root) return;
    for (const element of root.querySelectorAll('[data-artifact-media]')) {
        if (element.dataset.loaded === 'true') continue;
        element.dataset.loaded = 'pending';
        try {
            const blob = await fetchAgentArtifactBlob(element.dataset.artifactMedia);
            element.src = URL.createObjectURL(blob);
            element.dataset.loaded = 'true';
        } catch (error) {
            element.dataset.loaded = 'error';
            element.setAttribute('aria-label', error.message || '附件加载失败');
        }
    }
}

async function downloadAgentArtifact(artifactId, filename) {
    try {
        const blob = await fetchAgentArtifactBlob(artifactId);
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename || 'download';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
        alert('附件下载失败：' + (error.message || '请稍后重试'));
    }
}

// 兼容旧函数名；结果统一进入普通 Markdown/资源消息。
function addAgentResultCard(event) {
    return addStandardAgentResultMessage(event);
}

function recordAgentConversationTurn(event, cardDomId, content) {
    if (!content) return;
    const history = state.agentInteractionHistory[cardDomId] || [];
    const stateName = String(event.state || event.status || '').toLowerCase();
    if (['working', 'running', 'accepted', ''].includes(stateName)) return;
    const turnKey = `${event.task_id || event.taskId || ''}:${stateName}:${content}`;
    const isNewTurn = !history.some(turn => turn.key === turnKey);
    if (isNewTurn) {
        history.push({ key: turnKey, role: 'agent', content, state: stateName });
        state.agentInteractionHistory[cardDomId] = history;
    }
    // A fresh response re-enables the composer for the next round.  A local
    // re-render of the same response must keep the pending sending state.
    if (isNewTurn || !isAgentAwaitingInput(event)) delete state.agentInteractionState[cardDomId];
}

function renderAgentConversation(event, cardDomId) {
    const history = state.agentInteractionHistory[cardDomId] || [];
    if (history.length === 0) return '';
    const awaitingInput = isAgentAwaitingInput(event);
    const interaction = state.agentInteractionState[cardDomId] || {};
    const turns = history.map(turn => `
        <article class="agent-dialogue-turn agent-dialogue-${turn.role}">
            <div class="agent-dialogue-avatar">${turn.role === 'agent' ? '✦' : '你'}</div>
            <div class="agent-dialogue-bubble">
                <span class="agent-dialogue-label">${turn.role === 'agent' ? '智能体' : '你的补充'}</span>
                <div class="agent-dialogue-content">${renderRichMessage(turn.content)}</div>
            </div>
        </article>
    `).join('');

    let composer = '';
    if (awaitingInput) {
        const continuationKeydownAction = renderDynamicActionAttributes('agent-continuation-keydown', { cardDomId: String(cardDomId || '') });
        const continuationSubmitAction = renderDynamicActionAttributes('submit-agent-continuation', { cardDomId: String(cardDomId || '') });
        composer = `
            <div class="agent-dialogue-composer ${interaction.status === 'error' ? 'is-error' : ''}">
                <div class="agent-dialogue-composer-label">根据智能体的提示补充信息</div>
                <div class="agent-dialogue-input-row">
                    <textarea id="${escapeHTMLAttribute(`${cardDomId}-continue-input`)}" rows="1" ${interaction.status === 'pending' ? 'disabled' : ''}
                        placeholder="输入补充信息…（Ctrl / ⌘ + Enter 发送）"
                        ${continuationKeydownAction}></textarea>
                    <button type="button" class="agent-dialogue-send" ${interaction.status === 'pending' ? 'disabled' : ''}
                        ${continuationSubmitAction} aria-label="发送补充信息">
                        ${interaction.status === 'pending' ? '<span class="agent-dialogue-spinner"></span>' : '↑'}
                    </button>
                </div>
                <div class="agent-dialogue-helper ${interaction.status === 'pending' ? 'is-flowing' : ''}">${interaction.status === 'pending' ? '正在发送给智能体…' : (interaction.message || '智能体会基于你的补充继续处理。')}</div>
            </div>
        `;
    }

    return `
        <section class="agent-dialogue" data-agent-dialogue="${escapeHTMLAttribute(cardDomId)}">
            <div class="agent-dialogue-heading"><span>多轮对话</span><small class="${awaitingInput ? 'is-flowing' : ''}">${awaitingInput ? '等待你的补充' : '对话已完成'}</small></div>
            <div class="agent-dialogue-history">${turns}</div>
            ${composer}
        </section>
    `;
}

function handleAgentContinuationKeydown(event, cardDomId) {
    if (event.isComposing || event.keyCode === 229 || event.key === 'Process') return;
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        submitAgentContinuation(cardDomId);
    }
}

// 保留旧函数名，供历史页面或内嵌扩展调用。
function renderAgentContinuationPanel(event, cardDomId) {
    if (!isAgentAwaitingInput(event)) return '';
    const continuationSubmitAction = renderDynamicActionAttributes('submit-agent-continuation', { cardDomId: String(cardDomId || '') });
    return `
        <button type="button" ${continuationSubmitAction}>继续对话</button>
    `;
}

async function submitAgentContinuation(cardDomId) {
    const sourceEvent = getAgentResultEventForCard(cardDomId);
    const input = document.getElementById(`${cardDomId}-continue-input`);
    const status = document.getElementById(`${cardDomId}-continue-status`);
    const value = input?.value?.trim() || '';
    if (!sourceEvent) return;
    if (!value) {
        state.agentInteractionState[cardDomId] = { status: 'error', message: '请根据智能体的提示补充信息。' };
        addAgentResultCard(sourceEvent);
        return;
    }
    const history = state.agentInteractionHistory[cardDomId] || [];
    history.push({ key: `user:${Date.now()}`, role: 'user', content: value });
    state.agentInteractionHistory[cardDomId] = history;
    state.agentInteractionState[cardDomId] = { status: 'pending' };
    addAgentResultCard(sourceEvent);
    try {
        await continueAgentTask(sourceEvent, value);
    } catch (error) {
        state.agentInteractionState[cardDomId] = { status: 'error', message: `发送失败：${error.message}` };
        addAgentResultCard(sourceEvent);
    }
}

// 根据用户输入判断本轮是否是天气意图，用于把结果呈现切到天气应用模式。
function detectWeatherTaskIntent(text) {
    return /天气|气温|温度|降水|降雨|下雨|湿度|风速|weather|temperature|rain/i.test(String(text || ''));
}

// 从待调用 Agent 列表中重建天气意图，覆盖“聊天发现”和“执行调用”分成两次请求造成的状态丢失。
function detectWeatherIntentFromAgents(agents) {
    return (Array.isArray(agents) ? agents : []).some(agent => {
        return detectWeatherTaskIntent(getAgentCapabilityText(agent));
    });
}

// 依据事件与当前任务上下文决定是否应打开应用弹窗，并返回命中的预设。
function getRoutableAgentAppPreset(event, content) {
    const preset = resolveAgentAppPreset(event, content || '', { allowFallback: false });
    if (preset) return preset;

    if (!state.currentTaskIntent.preferredPresetId) return null;
    const preferredPreset = getAgentAppPresetById(state.currentTaskIntent.preferredPresetId);
    if (preferredPreset?.autoOpen === false) return null;
    const text = getAgentAppPresetText(event, content || '');
    if (preferredPreset?.kind === 'weather' && /searcher|搜索|检索|weather|天气/i.test(text)) {
        return preferredPreset;
    }
    if (preferredPreset?.kind === 'weather') return null;
    return preferredPreset?.matcher?.(event, content || '') ? preferredPreset : null;
}

// 根据预设和返回内容决定应用标题，天气模板会进一步提取城市名。
function getAgentAppTitle(preset, event, content) {
    if (typeof preset?.buildTitle === 'function') {
        return preset.buildTitle(event, content);
    }
    return preset?.title || event.agent_name || event.agent || '智能体应用';
}

// 渲染弹窗内的预设清单，让演示现场能看清还有哪些应用模板可托管 Agent 结果。
function renderAgentAppPresetList(activePreset) {
    if (!elements.agentAppPresetList) return;
    elements.agentAppPresetList.innerHTML = AGENT_APP_PRESETS.map(preset => {
        const isActive = preset.id === activePreset?.id;
        const modeLabel = preset.autoOpen === false ? '手动兜底' : '可自动启动';
        return `
            <button class="agent-app-preset-item ${isActive ? 'active' : ''}" type="button" disabled data-preset-id="${escapeHTMLAttribute(preset.id)}">
                <span class="agent-app-preset-kind">${escapeHTML(preset.shortTitle || preset.kind)}</span>
                <strong>${escapeHTML(preset.id)}</strong>
                <small>${escapeHTML(preset.statusLabel || '')} · ${escapeHTML(preset.supportLabel || modeLabel)}</small>
            </button>
        `;
    }).join('');
    if (elements.agentAppPresetSummary) {
        elements.agentAppPresetSummary.textContent = activePreset
            ? `${activePreset.shortTitle || activePreset.title}模板运行中`
            : '等待模板匹配';
    }
}

// 清理全屏 Agent App 的页面缓存；对话重置或退出登录时防止旧 Task 页面残留。
function resetAgentAppPages() {
    state.weatherAgentApp.pages = {};
    state.weatherAgentApp.order = [];
    state.weatherAgentApp.activePageKey = '';
    state.weatherAgentApp.lastCardDomId = '';
    state.weatherAgentApp.currentPresetId = '';
    state.weatherAgentApp.active = false;
    if (state.weatherAgentApp.launchTimer) {
        clearTimeout(state.weatherAgentApp.launchTimer);
        state.weatherAgentApp.launchTimer = null;
    }
    elements?.capabilityLaunchOverlay?.classList.remove('active');
    elements?.weatherAgentAppOverlay?.classList.remove('active');
    if (elements?.agentAppPageList) elements.agentAppPageList.innerHTML = '';
    if (elements?.agentAppPageSummary) elements.agentAppPageSummary.textContent = '暂无已打开应用';
    updateRecentAgentAppEntry();
}

// 为某次 AIP 返回生成稳定的应用页 key；同一个 task/card 更新同一页，不同 Agent 不互相覆盖。
function getAgentAppPageKey(event, preset) {
    const presetId = preset?.id || 'markdown.mc.v1';
    const cardRef = getAgentResultCardDomId(event).replace(/^agent-result-/, '');
    return `${presetId}-${cardRef}`
        .toLowerCase()
        .replace(/[^a-z0-9_.-]+/g, '-')
        .replace(/-+/g, '-')
        .replace(/^-|-$/g, '') || `agent-app-${Date.now()}`;
}

// 合并应用页的用户查询上下文；继续输入优先保留，避免结果页标题退回泛化任务。
function buildAgentAppUserQueryContext(event, previousEvent, continueInput) {
    const parts = [
        event.user_query_context,
        event.userQueryContext,
        event.user_query,
        event.userQuery,
        continueInput,
        event.assigned_task,
        event.assignedTask,
        previousEvent.continue_input,
        previousEvent.continueInput,
        previousEvent.assigned_task,
        previousEvent.assignedTask,
        previousEvent.user_query_context,
        previousEvent.userQueryContext,
        previousEvent.user_query,
        previousEvent.userQuery,
        state.currentTaskIntent.queryText
    ].filter(value => value !== undefined && value !== null && String(value).trim() !== '');

    return Array.from(new Set(parts.map(value => String(value).trim()))).join('\n');
}

// 记录或更新一个全屏应用页，确保连续返回的多个 Agent 都能在应用页内切换。
function rememberAgentAppPage(event, preset, content) {
    const resolvedPreset = preset || getAgentAppPresetById('markdown.mc.v1');
    const pageKey = getAgentAppPageKey(event, resolvedPreset);
    const previousPage = state.weatherAgentApp.pages[pageKey];
    const previousEvent = previousPage?.event || {};
    const baseCardDomId = getAgentResultCardDomId(event).replace(/^agent-result-/, '');
    const appSurfaceId = `agent-app-${resolvedPreset?.kind || 'generic'}-${baseCardDomId}`;
    const continueInput = event.continue_input || event.continueInput || event.user_input || event.userInput || previousEvent.continue_input || previousEvent.continueInput || '';
    const contextualEvent = {
        ...previousEvent,
        ...event,
        continue_input: continueInput,
        user_query_context: buildAgentAppUserQueryContext(event, previousEvent, continueInput),
        preferred_preset_context: event.preferred_preset_context || event.preferredPresetContext || previousEvent.preferred_preset_context || previousEvent.preferredPresetContext || state.currentTaskIntent.preferredPresetId
    };
    const page = {
        key: pageKey,
        presetId: resolvedPreset.id,
        event: contextualEvent,
        content: content || '',
        surfaceId: appSurfaceId,
        updatedAt: Date.now(),
        previousState: previousEvent.state || previousEvent.status || ''
    };

    state.weatherAgentApp.pages[pageKey] = page;
    if (!state.weatherAgentApp.order.includes(pageKey)) {
        state.weatherAgentApp.order.push(pageKey);
    }
    state.weatherAgentApp.activePageKey = pageKey;
    state.weatherAgentApp.lastCardDomId = appSurfaceId;
    state.weatherAgentApp.currentPresetId = resolvedPreset.id || '';
    updateRecentAgentAppEntry(page);
    return page;
}

// 找到最近一次打开或更新的 Agent App 页面，用于关闭全屏窗口后在叮当入口回看结果。
function getLatestAgentAppPage() {
    const activePage = state.weatherAgentApp.pages[state.weatherAgentApp.activePageKey];
    if (activePage) return activePage;
    return [...state.weatherAgentApp.order]
        .reverse()
        .map(key => state.weatherAgentApp.pages[key])
        .find(Boolean) || null;
}

// 按“当前页优先、最近更新优先”读取已缓存的 Agent App 页面，用于渲染模式即时切换。
function getAgentAppPagesForRenderToggle() {
    const pages = [];
    const seen = new Set();
    const pushPage = page => {
        if (!page || seen.has(page.key)) return;
        seen.add(page.key);
        pages.push(page);
    };

    pushPage(state.weatherAgentApp.pages[state.weatherAgentApp.activePageKey]);
    [...state.weatherAgentApp.order]
        .reverse()
        .map(key => state.weatherAgentApp.pages[key])
        .sort((a, b) => (b?.updatedAt || 0) - (a?.updatedAt || 0))
        .forEach(pushPage);
    return pages;
}

// 将应用页缓存还原为可被 raw/A2UI 两种 renderer 共用的 Agent 事件，避免切换时丢失补充输入上下文。
function buildAgentResultEventFromAppPage(page) {
    if (!page?.event) return null;
    const content = page.content || extractAgentCardContent(page.event);
    return {
        ...page.event,
        content,
        render_hints: {
            ...getAgentRenderHints(page.event),
            restored_from_app_cache: true,
            template_id: page.presetId || getAgentRenderHints(page.event).template_id
        }
    };
}

// 收集当前可切换展示的 Agent 结果事件；应用页优先于普通事件缓存，因为它包含最新页面状态。
function getCachedAgentResultsForRenderToggle() {
    const records = new Map();
    getAgentAppPagesForRenderToggle().forEach(page => {
        const event = buildAgentResultEventFromAppPage(page);
        if (!event) return;
        records.set(getRawAgentResultMessageDomId(event), { event, force: true, source: 'app-page' });
    });

    Object.values(state.agentResultEvents || {}).forEach(event => {
        if (!event) return;
        const messageDomId = getRawAgentResultMessageDomId(event);
        if (!records.has(messageDomId)) {
            records.set(messageDomId, { event, force: false, source: 'event-cache' });
        }
    });
    return Array.from(records.values());
}

// 关闭实时渲染时，把已打开或已缓存的应用页即时转换为 ACPs/AIP 原始 Markdown 消息。
function restoreRenderedAgentResultsAsRawMessages() {
    const records = getCachedAgentResultsForRenderToggle();
    let renderedCount = 0;
    records.forEach(record => {
        if (addRawAgentResultMessage(record.event, { force: record.force })) {
            renderedCount += 1;
        }
    });
    return renderedCount;
}

// 开启实时渲染时，优先从已有应用页缓存恢复全屏 App；没有应用页时再用事件缓存重建展示卡。
function restoreRawAgentResultsAsRenderedView() {
    const pages = getAgentAppPagesForRenderToggle();
    if (pages.length > 0) {
        pages.forEach(page => {
            const event = buildAgentResultEventFromAppPage(page);
            if (event) removeRawAgentResultMessage(event);
        });
        updateRecentAgentAppEntry(pages[0]);
        activateAgentAppPage(pages[0].key);
        return true;
    }

    const records = getCachedAgentResultsForRenderToggle();
    for (const record of records) {
        const event = record.event;
        const content = extractAgentCardContent(event);
        const preset = getRoutableAgentAppPreset(event, content || '')
            || resolveAgentAppPreset(event, content || '', { allowFallback: false });
        if (preset && shouldOpenLocalAgentApp(event, preset, content || '')) {
            removeRawAgentResultMessage(event);
            openAgentApp(event, preset);
            return true;
        }
        if (shouldRenderInlineAgentResult(event, content || '')) {
            removeRawAgentResultMessage(event);
            addAgentResultCard(event);
            return true;
        }
    }
    return false;
}

// 更新叮当聊天入口里的“最近应用”按钮；它是系统层回看入口，不放进具体 Agent App 内部。
function updateRecentAgentAppEntry(page = getLatestAgentAppPage()) {
    if (!elements.recentAgentAppBtn) return;
    if (!page) {
        elements.recentAgentAppBtn.classList.add('is-hidden');
        return;
    }

    const preset = getAgentAppPresetById(page.presetId);
    const title = getAgentAppTitle(preset, page.event || {}, page.content || '');
    const stateLabel = formatAgentCardState(page.event?.state || page.event?.status);
    elements.recentAgentAppTitle.textContent = `回看${title}`;
    elements.recentAgentAppMeta.textContent = `${preset?.shortTitle || 'Agent'} · ${stateLabel || '已返回'} · 点击重新打开`;
    elements.recentAgentAppBtn.dataset.pageKey = page.key;
    elements.recentAgentAppBtn.classList.remove('is-hidden');
}

// 只隐藏入口层回看按钮，不删除页面缓存；后续新 AIP 结果会重新显示回看入口。
function dismissRecentAgentAppEntry(event) {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    if (!elements.recentAgentAppBtn) return;
    elements.recentAgentAppBtn.classList.add('is-hidden');
    delete elements.recentAgentAppBtn.dataset.pageKey;
}

// 从叮当入口重新打开最近的 Agent App，不重新发起 AIP 调用，只恢复已有结果页。
function reopenRecentAgentApp() {
    const pageKey = elements.recentAgentAppBtn?.dataset.pageKey || state.weatherAgentApp.activePageKey;
    const fallbackPage = getLatestAgentAppPage();
    if (pageKey && state.weatherAgentApp.pages[pageKey]) {
        activateAgentAppPage(pageKey);
    } else if (fallbackPage) {
        activateAgentAppPage(fallbackPage.key);
    }
}

// 渲染已打开应用页切换条；这是移动端 App 内的页面栈入口，不再依赖聊天流卡片。
function renderAgentAppPageList() {
    if (!elements.agentAppPageList) return;
    const pages = state.weatherAgentApp.order
        .map(key => state.weatherAgentApp.pages[key])
        .filter(Boolean);

    if (elements.agentAppPageSummary) {
        elements.agentAppPageSummary.textContent = pages.length
            ? `${pages.length} 个 Agent 应用已打开`
            : '暂无已打开应用';
    }

    elements.agentAppPageList.innerHTML = pages.map(page => {
        const preset = getAgentAppPresetById(page.presetId);
        const event = page.event || {};
        const isActive = page.key === state.weatherAgentApp.activePageKey;
        const title = getAgentAppTitle(preset, event, page.content || '');
        const stateLabel = formatAgentCardState(event.state || event.status);
        return `
            <button class="agent-app-page-item ${isActive ? 'active' : ''}" type="button" data-page-key="${escapeHTMLAttribute(page.key)}">
                <span>${escapeHTML(preset?.shortTitle || '通用')}</span>
                <strong>${escapeHTML(title)}</strong>
                <small>${escapeHTML(stateLabel)} · ${escapeHTML(getCapabilityAgentName(event, preset))}</small>
            </button>
        `;
    }).join('');
}

// 获取当前全屏应用页对应的 Agent 结果卡片 ID，用于多轮输入状态定位。
function getActiveAgentAppCardRef() {
    const page = state.weatherAgentApp.pages[state.weatherAgentApp.activePageKey];
    if (!page?.event) return '';
    return getAgentResultCardDomId(page.event);
}

// 从缓存或当前应用页找回可继续的 AIP 事件，避免页面重绘后按钮失去任务上下文。
function getAgentResultEventForCard(cardRef) {
    if (cardRef && state.agentResultEvents[cardRef]) return state.agentResultEvents[cardRef];
    const page = state.weatherAgentApp.pages[state.weatherAgentApp.activePageKey];
    if (page?.event && (!cardRef || getAgentResultCardDomId(page.event) === cardRef)) {
        return page.event;
    }
    return null;
}

// 更新全屏应用的多轮交互状态；pending/error 不写进聊天流，直接在应用页反馈。
function setAgentAppContinueState(cardRef, nextState) {
    const ref = cardRef || getActiveAgentAppCardRef();
    if (!ref) return;
    state.weatherAgentApp.continuingByCard[ref] = {
        ...nextState,
        updatedAt: Date.now()
    };
    if (state.weatherAgentApp.active && getActiveAgentAppCardRef() === ref) {
        renderActiveAgentAppPage();
    }
}

// 清理某次多轮补充的临时状态；最终结果由后续 agent_result_card 重绘。
function clearAgentAppContinueState(cardRef) {
    const ref = cardRef || getActiveAgentAppCardRef();
    if (!ref) return;
    delete state.weatherAgentApp.continuingByCard[ref];
    if (state.weatherAgentApp.active && getActiveAgentAppCardRef() === ref) {
        renderActiveAgentAppPage();
    }
}

// 渲染天气应用级多轮面板；它位于全屏 App 顶部，不再混在天气结果卡片内部。
function renderAgentAppInteractionPanel(page, weather) {
    if (!elements.agentAppInteractionPanel) return;
    const cardRef = page?.event ? getAgentResultCardDomId(page.event) : '';
    const interactionState = cardRef ? state.weatherAgentApp.continuingByCard[cardRef] : null;
    const needsInput = Boolean(weather?.needsInput);
    const pending = interactionState?.status === 'pending';
    const errored = interactionState?.status === 'error';

    if (!weather || (!needsInput && !pending && !errored)) {
        elements.agentAppInteractionPanel.classList.add('is-hidden');
        clearDynamicActionElement(elements.agentAppInteractionPanel);
        delete elements.agentAppInteractionPanel.dataset.status;
        return;
    }

    elements.agentAppInteractionPanel.classList.remove('is-hidden');
    elements.agentAppInteractionPanel.dataset.status = pending ? 'pending' : errored ? 'error' : 'awaiting-input';

    if (pending) {
        replaceDynamicActionHTML(elements.agentAppInteractionPanel, `
            <div class="agent-app-interaction-copy">
                <span>ACPs 多轮对话</span>
                <strong>正在向天气智能体发送补充信息</strong>
                <em>${escapeHTML(interactionState.input ? `补充：${interactionState.input}` : '等待 AIP 返回结果...')}</em>
            </div>
            <div class="agent-app-interaction-spinner" aria-hidden="true"></div>
        `);
        return;
    }

    const inputId = `${cardRef || 'weather-card'}-app-location-input`;
    const weatherKeydownAction = renderDynamicActionAttributes('weather-input-keydown', { cardRef: String(cardRef || '') });
    const weatherSubmitAction = renderDynamicActionAttributes('submit-weather-input', { cardRef: String(cardRef || '') });
    const weatherLocationAction = renderDynamicActionAttributes('use-weather-location', { cardRef: String(cardRef || '') });
    const prompt = errored
        ? interactionState.message || '补充天气位置失败，请重新输入城市。'
        : weather.inputPrompt || '天气智能体需要补充城市，或授权使用当前位置后继续查询。';
    replaceDynamicActionHTML(elements.agentAppInteractionPanel, `
        <div class="agent-app-interaction-copy">
            <span>${errored ? '继续调用失败' : 'ACPs 多轮对话'}</span>
            <strong>${errored ? '请重新补充城市' : '天气智能体需要更多信息'}</strong>
            <em>${escapeHTML(prompt)}</em>
        </div>
        <div class="agent-app-interaction-controls">
            <input
                id="${escapeHTMLAttribute(inputId)}"
                class="weather-location-input"
                type="text"
                placeholder="例如：广州"
                value="${escapeHTMLAttribute(errored && interactionState.input ? interactionState.input : '')}"
                ${weatherKeydownAction}
            >
            <button class="weather-input-submit" type="button" ${weatherSubmitAction}>继续</button>
            <button class="weather-use-location" type="button" ${weatherLocationAction} ${weather.canUseLocation ? '' : 'disabled'}>
                使用当前位置
            </button>
        </div>
    `);
}

// 将当前激活的 Agent 应用页绘制到全屏容器；内容区是唯一滚动区。
function renderActiveAgentAppPage(options = {}) {
    const page = state.weatherAgentApp.pages[state.weatherAgentApp.activePageKey];
    if (!page || !elements.weatherAgentAppOverlay || !elements.weatherAgentAppSurface) return false;

    const resolvedPreset = getAgentAppPresetById(page.presetId);
    const event = page.event || {};
    const content = page.content || '';
    const weather = resolvedPreset?.kind === 'weather' ? buildWeatherCardData(event, content) : null;

    if (elements.weatherAgentAppKicker) elements.weatherAgentAppKicker.textContent = resolvedPreset?.kicker || 'A2UI / MC Agent App';
    elements.weatherAgentAppTitle.textContent = getAgentAppTitle(resolvedPreset, event, content);
    elements.weatherAgentAppState.textContent = formatAgentCardState(event.state || event.status);
    elements.weatherAgentAppAgent.textContent = formatScopedAgentName(event, resolvedPreset);
    elements.weatherAgentAppTask.textContent = event.task_id || event.taskId || 'Task Pending';
    if (elements.agentAppInvocationSummary) {
        const deployment = getAgentDeploymentScope(event);
        elements.agentAppInvocationSummary.textContent = formatAgentInvocationSummary(event, resolvedPreset);
        elements.agentAppInvocationSummary.dataset.scope = deployment.scope;
    }
    renderAgentAppPageList();
    renderAgentAppPresetList(resolvedPreset);
    renderAgentAppInteractionPanel(page, weather);
    replaceDynamicActionHTML(elements.weatherAgentAppSurface, renderA2uiAgentSurface(
        event,
        content || `正在启动${resolvedPreset?.shortTitle || '智能体'}应用...`,
        page.surfaceId,
        resolvedPreset
    ));
    hydrateWeatherCityVisuals(elements.weatherAgentAppSurface);
    elements.weatherAgentAppEvidence.innerHTML = `
        <div class="weather-agent-app-evidence-row">${renderAgentCardEvidence(event)}</div>
        ${renderRawTaskResult(event)}
    `;
    if (!options.preserveScroll) {
        requestAnimationFrame(() => {
            if (elements.weatherAgentAppBody) {
                elements.weatherAgentAppBody.scrollTop = 0;
            }
        });
    }
    return true;
}

// 切换到某个已打开的 Agent 应用页，供顶部页面栈按钮使用。
function activateAgentAppPage(pageKey) {
    if (!state.weatherAgentApp.pages[pageKey]) return;
    hideCapabilityLaunch();
    state.weatherAgentApp.activePageKey = pageKey;
    if (renderActiveAgentAppPage()) {
        elements.weatherAgentAppOverlay?.classList.add('active');
        state.weatherAgentApp.active = true;
    }
}

// 从 Agent 名称或任务中提取启动弹窗里展示的能力名。
function getCapabilityAgentName(event, preset = null) {
    return event.agent_name || event.agent || `${preset?.shortTitle || '云端'}智能体`;
}

// Agent 身份只在这一处组合，流程卡、启动动画和 A2UI 应用页必须展示同一份真实名称。
function formatScopedAgentName(event, preset = null) {
    const deployment = getAgentDeploymentScope(event);
    return `${deployment.selectionLabel}：${getCapabilityAgentName(event, preset)}`;
}

// 展示短暂的能力启动弹窗，表达“找到智能体 -> 启动应用”的系统级动作。
function showCapabilityLaunch(event, preset, appData = {}) {
    if (!elements.capabilityLaunchOverlay) return;
    const deployment = getAgentDeploymentScope(event);
    const isLocalWeatherApp = deployment.scope === 'local' && preset?.kind === 'weather';
    elements.capabilityLaunchTitle.textContent = isLocalWeatherApp
        ? '已找到本地天气 App 智能体'
        : deployment.scope === 'local'
            ? `已找到本地${preset?.shortTitle || ''}应用智能体`
            : `已找到云端${preset?.shortTitle || ''}智能体`;
    elements.capabilityLaunchSubtitle.textContent = formatAgentInvocationSummary(event, preset, { loading: true });
    if (elements.capabilityLaunchAgent) {
        elements.capabilityLaunchAgent.textContent = 'AIP';
    }
    if (elements.capabilityLaunchTarget) {
        elements.capabilityLaunchTarget.innerHTML = `${escapeHTML(getCapabilityAgentName(event, preset))}<br><small>${escapeHTML(deployment.selectionLabel)}</small>`;
    }
    elements.capabilityLaunchOverlay.classList.add('active');
}

// 隐藏能力启动弹窗；如果有延迟启动计时器，一并清理。
function hideCapabilityLaunch() {
    if (state.weatherAgentApp.launchTimer) {
        clearTimeout(state.weatherAgentApp.launchTimer);
        state.weatherAgentApp.launchTimer = null;
    }
    elements.capabilityLaunchOverlay?.classList.remove('active');
}

// 关闭智能体应用并回到叮当聊天入口，A2UI surface 状态保留便于同一任务后续更新。
function closeAgentApp() {
    hideCapabilityLaunch();
    elements.weatherAgentAppOverlay?.classList.remove('active');
    state.weatherAgentApp.active = false;
    switchTab('chat');
    scrollToBottom();
}

// 顺序工作流进入下一阶段前强制收口天气页。后端已经给出最终天气内容时，
// 即便某个 WebView 丢失了 final state 字段，也不能继续显示天气加载光条。
function finalizeWeatherAgentAppForHandoff(event) {
    const expectedCardId = String(event.card_id || event.cardId || '');
    const expectedTaskId = String(event.task_id || event.taskId || '');
    const page = Object.values(state.weatherAgentApp.pages || {}).find(candidate => {
        const candidateEvent = candidate?.event || {};
        const candidateCardId = String(candidateEvent.card_id || candidateEvent.cardId || '');
        const candidateTaskId = String(candidateEvent.task_id || candidateEvent.taskId || '');
        return (expectedCardId && candidateCardId === expectedCardId)
            || (expectedTaskId && candidateTaskId === expectedTaskId)
            || candidate?.presetId === 'weather.summary.v1';
    });
    if (!page) return false;

    page.event = {
        ...page.event,
        state: 'completed',
        status: 'success',
        task_id: expectedTaskId || page.event?.task_id || '',
    };
    page.updatedAt = Date.now();
    if (state.weatherAgentApp.activePageKey === page.key) {
        renderActiveAgentAppPage({ preserveScroll: true });
    }
    return true;
}

// 用 execution_complete / execution_error 再次收口第二轮应用页。
// 正常情况下 agent_result_card 已先更新同一 cardId；这里负责处理丢卡、空 products 和终态事件乱序。
function finalizeAgentAppForTerminalEvent(event) {
    const expectedCardId = String(event.card_id || event.cardId || '');
    const expectedTaskId = String(event.task_id || event.taskId || '');
    const terminalFailed = event.type === 'execution_error'
        || ['failed', 'error', 'rejected', 'canceled', 'cancelled'].includes(String(event.state || event.status || '').toLowerCase());
    const pages = Object.values(state.weatherAgentApp.pages || {});
    const page = pages.find(candidate => {
        const candidateEvent = candidate?.event || {};
        return (expectedCardId && String(candidateEvent.card_id || candidateEvent.cardId || '') === expectedCardId)
            || (expectedTaskId && String(candidateEvent.task_id || candidateEvent.taskId || '') === expectedTaskId);
    });
    if (!page) return false;

    page.event = {
        ...page.event,
        ...event,
        agent: event.agent || page.event?.agent || '目标智能体',
        agent_name: event.agent_name || event.agent || page.event?.agent_name || '目标智能体',
        task_id: expectedTaskId || page.event?.task_id || '',
        state: terminalFailed ? 'failed' : 'completed',
        status: terminalFailed ? 'error' : 'success'
    };
    if (typeof event.content === 'string' && event.content.trim()) {
        page.content = event.content;
    }
    page.updatedAt = Date.now();
    rememberAgentResultEvent(page.event);
    if (state.weatherAgentApp.activePageKey === page.key) {
        renderActiveAgentAppPage({ preserveScroll: true });
    }
    updateRecentAgentAppEntry(page);
    return true;
}

// 工作流交接由后续的 agent_result_card 唤起第二个协调应用页。
async function handleWorkflowHandoff(event) {
    const handoffId = String(event.handoff_id || event.handoffId || '');
    if (!handoffId || state.workflowHandoffs[handoffId]) return;
    state.workflowHandoffs[handoffId] = 'transitioning';

    const firstRoundAgentName = event.agent || '前序智能体';
    const nextAction = event.next_action || event.nextAction || {};
    const targetAgentName = nextAction.agent?.name || '目标智能体';
    addMessageToUI({
        role: 'assistant',
        content: event.message || `好的，我已经拿到本地智能体 ${firstRoundAgentName} 的天气结果了。接下来我会在第二轮通过 Discovery 查找并调用云端智能体 ${targetAgentName}。`
    });
    await new Promise(resolve => window.setTimeout(resolve, 300));

    if (!['direct-invoke', 'discover-and-direct-invoke'].includes(nextAction.kind)
        || !nextAction.task
        || (!nextAction.discovery_query && !nextAction.agent)) {
        state.workflowHandoffs[handoffId] = 'failed';
        throw new Error('叮当未收到可执行的第二轮调用信息');
    }

    createProcessCard(
        event.conversation_id || state.currentConversationId,
        `第 2 轮：发现并调用云端智能体 ${targetAgentName}`,
        {
            title: '第 2 轮 · 云端智能体调用',
            identity: `云端智能体：${targetAgentName}`,
            stages: [
                { index: 0, icon: '🌦️', title: '本地天气结果', defaultStatus: '待接收' },
                { index: 1, icon: '🔍', title: '云端 Agent 发现', defaultStatus: '待执行' },
                { index: 2, icon: '⚡', title: '云端 AIP 调用', defaultStatus: '待执行' },
                { index: 3, icon: '✅', title: '云端结果返回', defaultStatus: '待执行' }
            ]
        }
    );
    const secondRoundCardId = state.currentProcessCard?.id;
    if (secondRoundCardId) {
        updateProcessStage(secondRoundCardId, 0, 'completed', {
            detail: `本地智能体：${escapeHTML(firstRoundAgentName)} 已完成并返回天气结果`
        });
        updateProcessStage(secondRoundCardId, 1, 'running', {
            detail: `正在通过 ACPs Discovery 查找云端智能体：${escapeHTML(targetAgentName)}…`
        });
    }

    state.workflowHandoffs[handoffId] = 'discovering';
    // 第二轮的结果呈现状态必须独立计数，不能继承第一轮天气 App 已经打开过的标志。
    state.hasAgentResultCardsInCurrentRun = false;
    state.hasRawAgentResultsInCurrentRun = false;
    const response = await fetch(`${API_BASE}/agent/direct-invoke`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getAuthHeaders()
        },
        body: JSON.stringify({
            task: nextAction.task,
            conversation_id: event.conversation_id || state.currentConversationId,
            card_id: nextAction.card_id || '',
            workflow_handoff_id: handoffId,
            discovery_query: nextAction.discovery_query,
            discovery_target: nextAction.discovery_target || '',
            agent: nextAction.agent || null
        })
    });
    if (!response.ok) {
        state.workflowHandoffs[handoffId] = 'failed';
        throw await createHttpError(response, '发起第二轮调用');
    }

    applyQuotaHeaders(response);
    const terminalEvent = await processExecutionStream(response);
    const targetIdentity = terminalEvent?.agent_name || terminalEvent?.agent || targetAgentName;
    if (!terminalEvent || terminalEvent.type === 'execution_error') {
        state.workflowHandoffs[handoffId] = 'failed';
        if (!terminalEvent && secondRoundCardId) {
            updateProcessStage(secondRoundCardId, 2, 'error', { detail: '第二轮响应提前结束，未收到执行终态' });
            updateProcessStage(secondRoundCardId, 3, 'error', { detail: '未收到云端智能体的最终结果' });
        }
        addMessageToUI({
            role: 'assistant',
            content: terminalEvent
                ? `云端智能体 ${targetIdentity} 执行失败：${terminalEvent.message || terminalEvent.content || '未返回可用结果'}`
                : `云端智能体 ${targetAgentName} 的第二轮响应提前结束，未收到终态结果。`
        });
        return;
    }
    state.workflowHandoffs[handoffId] = 'completed';
    addMessageToUI({
        role: 'assistant',
        content: `云端智能体 ${targetIdentity} 已执行完成并返回结果${terminalEvent.task_id ? `（Task ${terminalEvent.task_id}）` : ''}，结果应用已更新。`
    });
}

// 兼容旧事件绑定名；实际关闭的是通用智能体应用。
function closeWeatherAgentApp() {
    closeAgentApp();
}

// 判断当前卡片是否来自 Partner 误把追问作为 completed 返回的兼容场景。
function shouldRestartAgentTask(event) {
    const hints = getAgentRenderHints(event);
    const originalState = String(hints.protocol_state_original || hints.protocolStateOriginal || '').toLowerCase();
    return hints.interaction_reason === 'weather-location-required'
        || hints.interactionReason === 'weather-location-required'
        || originalState === 'completed';
}

// 将天气应用中的用户补充输入回传给后端，由后端选择 AIP continue 或重启任务。
async function continueAgentTask(event, userInput, options = {}) {
    const cardRef = event ? getAgentResultCardDomId(event) : getActiveAgentAppCardRef();
    if (!event) {
        setAgentAppContinueState(cardRef, {
            status: 'error',
            message: '找不到可继续的 AIP 任务，请重新发起调用。'
        });
        return;
    }
    const input = String(userInput || '').trim();
    if (!input) {
        setAgentAppContinueState(cardRef, {
            status: 'error',
            message: '请先补充智能体所需的信息。'
        });
        return;
    }

    setAgentAppContinueState(cardRef, {
        status: 'pending',
        input,
        message: `正在向${event.agent_name || event.agent || '智能体'}发送补充信息...`
    });

    try {
        const context = await buildClientContext({ requireLocation: options.requireLocation || false });
        const payload = {
            conversation_id: event.conversation_id || state.currentConversationId,
            agent: {
                name: event.agent_name || event.agent || '未知',
                endpoint: event.endpoint || '',
                aic: event.aic || ''
            },
            endpoint: event.endpoint || '',
            aic: event.aic || '',
            task_id: event.task_id || event.taskId || '',
            session_id: event.session_id || event.sessionId || '',
            card_id: event.card_id || event.cardId || '',
            assigned_task: event.assigned_task || '',
            user_input: input,
            force_new_task: Boolean(options.forceNewTask || shouldRestartAgentTask(event)),
            quota_continuation_token: event.quota_continuation_token || event.quotaContinuationToken || '',
            client_context: context
        };

        const response = await fetch(`${API_BASE}/agent/continue`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...getAuthHeaders()
            },
            body: JSON.stringify(payload),
            signal: options.signal
        });

        if (!response.ok) {
            throw new Error('Failed to continue agent task');
        }

        applyQuotaHeaders(response);
        await processExecutionStream(response);
        clearAgentAppContinueState(cardRef);
    } catch (error) {
        setAgentAppContinueState(cardRef, {
            status: 'error',
            input,
            message: `继续智能体调用失败：${error.message}`
        });
        throw error;
    }
}

// 回车提交天气卡片中的城市输入，保持移动端和桌面端操作一致。
function handleWeatherInputKeydown(event, cardRef) {
    if (event.isComposing || event.keyCode === 229 || event.key === 'Process') return;
    if (event.key !== 'Enter') return;
    event.preventDefault();
    submitWeatherAgentInput(cardRef);
}

// 从天气应用卡片读取城市并继续 AIP 多轮任务。
async function submitWeatherAgentInput(cardRef) {
    const sourceEvent = getAgentResultEventForCard(cardRef);
    const input = document.getElementById(`${cardRef}-app-location-input`) || document.getElementById(`${cardRef}-location-input`);
    const value = input?.value?.trim() || '';
    try {
        await continueAgentTask(sourceEvent, value, { requireLocation: false });
    } catch (error) {
        console.error('Continue weather task failed:', error);
    }
}

// 使用浏览器当前位置作为天气 Partner 的补充输入；拿不到定位时仍会回到城市输入态。
async function useCurrentLocationForWeatherAgent(cardRef) {
    const sourceEvent = getAgentResultEventForCard(cardRef);
    try {
        await continueAgentTask(sourceEvent, '使用当前位置', { requireLocation: true });
    } catch (error) {
        console.error('Continue weather task with location failed:', error);
    }
}

// 把 Agent 的 AIP 返回渲染到全屏应用，而不是插入聊天消息卡片。
function openAgentApp(event, preset) {
    return addStandardAgentResultMessage(event);
}

// 兼容旧天气入口；外部旧调用会自动走通用应用管理器。
function openWeatherAgentApp(event) {
    openAgentApp(event, getAgentAppPresetById('weather.summary.v1'));
}

// 根据 Agent 结果类型选择呈现方式：只有本机天气 App 进入全屏应用，其他 Agent 继续留在聊天/流程卡语境。
function presentAgentResult(event) {
    return addStandardAgentResultMessage(event)
        ? 'agent-markdown-message'
        : 'agent-progress-only';
}

// 当前任务有明确应用意图时，其他 Agent 的泛化寒暄或兜底回复不应抢占全屏应用页。
function shouldSuppressNonPreferredAgentApp(event, preset, content) {
    let preferredPreset = state.currentTaskIntent.preferredPresetId
        ? getAgentAppPresetById(state.currentTaskIntent.preferredPresetId)
        : null;
    if (!preferredPreset && (state.currentTaskIntent.weather || detectWeatherTaskIntent(state.currentTaskIntent.queryText))) {
        preferredPreset = getAgentAppPresetById('weather.summary.v1');
    }
    if (!preferredPreset || preferredPreset.id === 'markdown.mc.v1' || preset.kind === preferredPreset.kind) return false;

    const text = [
        event.assigned_task,
        event.agent_name,
        event.agent,
        event.skill_id,
        event.skillId,
        content
    ].filter(Boolean).join('\n');
    return /感谢用户|感谢您的|如有其他需求|随时提问|可进一步查询|当前已处理|我可以帮助您的方向/.test(text);
}

// 复制消息功能
function copyMessage(btn, text) {
    navigator.clipboard.writeText(text).then(() => {
        btn.textContent = '✓';
        btn.classList.add('copied');
        setTimeout(() => {
            btn.textContent = '⎘';
            btn.classList.remove('copied');
        }, 1500);
    });
}

// 复制关闭实时渲染时的原始 AIP 返回；正文放在内存映射中，避免把大段 JSON 写进 HTML 属性。
function copyRawAgentResultMessage(btn, messageDomId) {
    copyMessage(btn, state.rawAgentResultCopyTexts[messageDomId] || '');
}

// 发送消息
async function sendMessage() {
    const text = elements.messageInput.value.trim();
    const hasImage = state.currentImage && state.currentImage.imageUrl;
    const hasAttachment = state.currentAttachments.some(item => Boolean(item.artifactId));
    if (!text && !hasImage && !hasAttachment) return;
    if (getConversationRuntime(state.currentConversationId).loading) return;
    if ((state.currentImage && state.currentImage.uploading) || state.currentAttachments.some(item => item.uploading)) {
        alert('附件正在上传中，请稍候...');
        return;
    }

    if (!state.user) {
        showAuthModal();
        return;
    }
    if (!await ensureFreshAccessToken()) {
        showAuthModal();
        return;
    }

    if (state.activeAgentInteraction) {
        if (hasImage || hasAttachment) {
            alert('多轮对话进行中，请先用文本补充信息，或结束本次对话后再上传附件。');
            return;
        }
        if (state.activeAgentInteraction.collaboration) {
            await continueSecretaryCollaboration(text, state.activeAgentInteraction);
        } else {
            await continueActiveAgentInteraction(text);
        }
        return;
    }

    if (state.pendingSecretaryCollaboration) {
        if (hasImage || hasAttachment) {
            alert('叮当正在等待文本补充，请先完成本轮协作。');
            return;
        }
        await continueSecretaryCollaboration(text);
        return;
    }

    const requestedConversationId = state.currentConversationId;
    const runtimeKey = conversationRuntimeKey(requestedConversationId) || `draft-${Date.now()}`;
    const requestController = new AbortController();
    getConversationRuntime(runtimeKey);
    setConversationLoading(runtimeKey, true, requestController);
    state.selectedIoaAgents.clear();
    state.currentWebAgents = [];
    state.currentIoaAgents = [];
    state.currentProcessCard = null;
    currentStreamingMessage = null;
    currentStreamingMeta = null;
    state.hasAgentResultCardsInCurrentRun = false;
    state.hasRawAgentResultsInCurrentRun = false;
    state.rawAgentResultCopyTexts = {};
    state.currentTaskIntent.weather = detectWeatherTaskIntent(text);
    state.currentTaskIntent.queryText = text;
    state.currentTaskIntent.preferredPresetId = detectAgentAppIntentPreset(text)?.id || '';
    state.currentTaskIntent.attachmentIds = [
        hasImage ? state.currentImage.artifactId : null,
        ...state.currentAttachments.map(item => item.artifactId)
    ].filter(Boolean);

    const userMessage = {
        role: 'user',
        content: text,
        image_url: hasImage ? state.currentImage.imageUrl : null
    };
    addMessageToUI(userMessage);
    elements.messageInput.value = '';
    elements.messageInput.style.height = 'auto';

    const imageUrl = hasImage ? state.currentImage.imageUrl : null;
    const attachmentIds = [...state.currentTaskIntent.attachmentIds];
    clearCurrentImage();
    clearCurrentAttachment();

    createProcessCard(state.currentConversationId, text);

    const typingId = addTypingIndicator();

    const streamContext = {
        requestedConversationId,
        runtimeKey,
    };
    try {
        await streamChatResponse(text, typingId, imageUrl, attachmentIds, requestController.signal, streamContext);
    } catch (e) {
        if (e.name === 'AbortError') return;
        console.error('Error sending message:', e);
        removeTypingIndicator(typingId);
        markCurrentProcessCardError(e.message || '发送消息失败');
        addErrorMessage(`抱歉，发生了错误：${e.message || '请稍后重试'}`);
    } finally {
        if (getConversationRuntime(streamContext.runtimeKey).activity === 'running') {
            setConversationActivity(streamContext.runtimeKey, 'attention');
        }
        setConversationLoading(streamContext.runtimeKey, false);
    }
}

async function continueSecretaryCollaboration(text, interaction = null) {
    const pending = state.pendingSecretaryCollaboration || interaction?.collaboration;
    if (!pending) return;
    const selectedAgents = Array.isArray(pending.selectedAgents)
        ? pending.selectedAgents.filter(Boolean)
        : [];
    if (selectedAgents.length === 0) {
        addErrorMessage('当前协作缺少已选智能体，无法继续；请重新发起智能体发现。');
        return;
    }

    const continuationInteraction = secretaryContinuationInteraction(pending, interaction);
    const execution = beginAgentContinuationExecution(continuationInteraction);
    addMessageToUI({ role: 'user', content: text });
    state.messages.push({ role: 'user', content: text });
    elements.messageInput.value = '';
    elements.messageInput.style.height = 'auto';
    // 用户已经提交补充，不能继续显示“正在等待你的回复”。
    clearActiveAgentInteraction();

    try {
        // The completion card has no native continue protocol. Starting a
        // preference-driven collaboration therefore supersedes its paused run.
        if (continuationInteraction.run_id) {
            await cancelAgentInteraction(continuationInteraction);
        }
        const clientContext = await buildClientContext({ requireLocation: false });
        const candidateContext = pending.candidateContent || pending.content || '';
        const resumedQuery = `${pending.userQuery}\n\n上一轮候选结果：${candidateContext}\n\n叮当请求补充：${pending.question}\n用户回答：${text}`;
        state.pendingSecretaryCollaboration = null;
        createSecretaryContinuationCard(state.currentConversationId, pending, text);
        const response = await fetch(`${API_BASE}/agent/execute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({
                conversation_id: String(state.currentConversationId || ''),
                selected_agents: selectedAgents,
                user_query: resumedQuery,
                continuation_user_input: text,
                ioa_agents: pending.ioaAgents,
                client_context: clientContext,
                attachment_ids: pending.attachmentIds || []
            }),
            signal: execution.controller.signal
        });
        if (!response.ok) throw await createHttpError(response, '继续协作');
        await processExecutionStream(response);
    } catch (error) {
        if (execution.controller.signal.aborted) return;
        state.pendingSecretaryCollaboration = continuationInteraction.collaboration;
        setActiveAgentInteraction(continuationInteraction);
        addErrorMessage(`继续叮当多智能体协调失败：${error.message || '请稍后重试'}`);
    } finally {
        finishAgentContinuationExecution(execution.conversationId, execution.controller);
        if (!state.pendingSecretaryCollaboration) elements.messageInput.placeholder = '输入消息...';
    }
}

function secretaryContinuationInteraction(pending, source = null) {
    const runId = String(pending?.runId || pending?.run_id || source?.run_id || '');
    const collaboration = {
        ...(pending || {}),
        runId,
    };
    return {
        interaction_kind: 'collaboration',
        conversation_id: state.currentConversationId,
        run_id: runId,
        execution_origin: 'dingdang-collaboration',
        collaboration_topology: pending?.collaborationTopology || source?.collaboration_topology || '',
        node_source: pending?.nodeSource || pending?.node_source || source?.node_source || 'dingdang',
        collaboration_task_id: pending?.collaborationTaskId || pending?.collaboration_task_id || source?.collaboration_task_id || '',
        task_id: pending?.taskId || pending?.task_id || source?.task_id || '',
        endpoint: pending?.endpoint || source?.endpoint || '',
        aic: pending?.aic || source?.aic || '',
        session_id: pending?.sessionId || pending?.session_id || source?.session_id || '',
        agent: pending?.agent || source?.agent || '叮当',
        agent_name: pending?.agent || pending?.agent_name || source?.agent_name || source?.agent || '叮当',
        state: 'awaiting-input',
        status: 'awaiting-input',
        cancelable: Boolean(runId),
        collaboration,
    };
}

async function confirmSecretaryCompletion() {
    const pending = state.pendingSecretaryCompletion;
    if (!pending || state.isLoading) return;
    state.isLoading = true;
    try {
        const completionMessage = {
            role: 'assistant',
            content: `已按你的确认完成协作。\n\n${pending.content}`
        };
        if (pending.nodeSource === 'external-agent' && pending.taskId && !pending.agentCompletionRecorded) {
            const response = await fetch(`${API_BASE}/agent/complete`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                body: JSON.stringify({
                    task_id: pending.taskId,
                    agent: pending.agent,
                    conversation_id: state.currentConversationId
                })
            });
            if (!response.ok) throw await createHttpError(response, '确认候选方案');
            pending.agentCompletionRecorded = true;
        }
        if (pending.runId) {
            const response = await fetch(`${API_BASE}/collaboration/${encodeURIComponent(pending.runId)}/complete`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                body: JSON.stringify({
                    node_id: pending.collaborationTaskId || 'secretary',
                    content: completionMessage.content
                })
            });
            if (!response.ok) throw await createHttpError(response, '确认候选方案');
        } else if (pending.taskId && pending.agent && pending.agent !== '叮当' && !pending.agentCompletionRecorded) {
            const response = await fetch(`${API_BASE}/agent/complete`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                body: JSON.stringify({
                    task_id: pending.taskId,
                    agent: pending.agent,
                    conversation_id: state.currentConversationId
                })
            });
            if (!response.ok) throw await createHttpError(response, '确认候选方案');
            pending.agentCompletionRecorded = true;
        }
        markCollaborationTaskCompleted(pending.runId, pending.collaborationTaskId, '用户已确认候选方案，协作完成。');
        removeConfirmedCompletionArtifacts(pending);
        state.pendingSecretaryCompletion = null;
        document.querySelectorAll('.secretary-completion-prompt').forEach(prompt => prompt.remove());
        clearInlineExecutionStatus();
        state.messages.push(completionMessage);
        addMessageToUI(completionMessage);
        clearActiveAgentInteraction();
        setConversationActivity(state.currentConversationId, 'idle');
    } catch (error) {
        addErrorMessage(`确认候选方案失败：${error.message || '请稍后重试'}`);
    } finally { state.isLoading = false; }
}

function markCollaborationTaskCompleted(runId, taskId, message) {
    if (!runId || !taskId) return;
    const run = state.collaborationRuns[runId];
    const task = run?.tasks.find(item => item.id === taskId);
    if (!run || !task) return;
    task.status = 'completed';
    task.message = message || '协作已完成。';
    run.detail = `${task.id === 'secretary' ? '叮当' : (task.agent || task.title)} · ${task.message}`;
    renderCollaborationRun(run);
}

function continueSecretaryCompletion() {
    const pending = state.pendingSecretaryCompletion;
    if (!pending) return;
    state.pendingSecretaryCompletion = null;
    const interaction = secretaryContinuationInteraction({
        ...(pending.collaboration || {}),
        runId: pending.runId,
        nodeSource: pending.nodeSource,
        collaborationTaskId: pending.collaborationTaskId,
        taskId: pending.taskId,
        agent: pending.agent,
        endpoint: pending.endpoint,
        aic: pending.aic,
        sessionId: pending.sessionId,
        content: pending.content,
        candidateContent: pending.content,
    });
    state.pendingSecretaryCollaboration = interaction.collaboration;
    setActiveAgentInteraction(interaction);
    addMessageToUI({ role: 'assistant', content: `好的，请补充你的偏好后继续协作。\n\n${pending.question || '请说明你希望调整的目标、约束或输出偏好。'}` });
}

async function continueActiveAgentInteraction(text) {
    const interaction = state.activeAgentInteraction;
    if (!interaction) return;

    const execution = beginAgentContinuationExecution(interaction);

    addMessageToUI({ role: 'user', content: text });
    state.messages.push({ role: 'user', content: text });
    elements.messageInput.value = '';
    elements.messageInput.style.height = 'auto';
    // 续办请求已经开始，立刻关闭旧的等待态；失败时才恢复。
    clearActiveAgentInteraction();

    try {
        await continueAgentTask(interaction, text, { signal: execution.controller.signal });
    } catch (error) {
        if (execution.controller.signal.aborted) return;
        console.error('Continue active agent interaction failed:', error);
        setActiveAgentInteraction(interaction);
        const errorText = normalizeUserFacingErrorText(error.message);
        if (elements.agentInteractionStatusHint) elements.agentInteractionStatusHint.textContent = `发送失败：${errorText}`;
        addErrorMessage(`继续智能体对话失败：${errorText}`);
    } finally {
        finishAgentContinuationExecution(execution.conversationId, execution.controller);
    }
}

async function cancelAgentInteraction(interaction) {
    const agentName = interaction.agent_name || interaction.agent || '智能体';
    if (interaction.interaction_kind === 'collaboration') {
        const collaborationTasks = Array.isArray(interaction.collaboration_tasks)
            ? interaction.collaboration_tasks.filter(task => task && task.task_id)
            : [];
        const response = await fetch(`${API_BASE}/collaboration/${encodeURIComponent(interaction.run_id || '')}/cancel`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({
                task_id: interaction.node_source === 'external-agent' ? (interaction.task_id || interaction.taskId || '') : '',
                tasks: collaborationTasks,
                agent: interaction.agent_name || interaction.agent || '',
                endpoint: interaction.endpoint || '',
                aic: interaction.aic || '',
                session_id: interaction.session_id || interaction.sessionId || ''
            })
        });
        if (!response.ok) throw await createHttpError(response, '结束协作');
        return;
    }
    if (interaction.cancelable === false || !(interaction.task_id || interaction.taskId)) {
        throw new Error('该历史任务已结束或不可继续，不能取消。');
    }
    const response = await fetch(`${API_BASE}/agent/cancel`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({
                conversation_id: String(interaction.conversation_id || state.currentConversationId || ''),
                agent: {
                    name: agentName,
                    endpoint: interaction.endpoint || '',
                    aic: interaction.aic || ''
                },
                endpoint: interaction.endpoint || '',
                aic: interaction.aic || '',
                task_id: interaction.task_id || interaction.taskId || '',
                session_id: interaction.session_id || interaction.sessionId || ''
            })
        });
    if (!response.ok) throw await createHttpError(response, '结束多轮对话');
}

function markAgentInteractionCancelled(interaction) {
    const agentName = interaction.agent_name || interaction.agent || '智能体';
    const pollCount = Math.max(Number(interaction.poll_count ?? interaction.pollCount ?? 0), agentPollCounts[agentName] || 0);
    const elapsedTime = Math.max(Number(interaction.elapsed_time ?? interaction.elapsedTime ?? 0), agentElapsedTimes[agentName] || 0);
    updateAgentStatus(agentName, 'canceled', pollCount, elapsedTime);
    if (statusUpdateInterval) {
        clearInterval(statusUpdateInterval);
        statusUpdateInterval = null;
    }
    executionStartTime = null;
}

async function endActiveAgentInteraction() {
    const interaction = state.activeAgentInteraction;
    if (!interaction || state.isLoading) return;
    const agentName = interaction.agent_name || interaction.agent || '智能体';
    if (!confirm(`确定结束与 ${agentName} 的本次多轮对话吗？`)) return;

    state.isLoading = true;
    elements.agentInteractionEndBtn.disabled = true;
    try {
        await cancelAgentInteraction(interaction);
        markAgentInteractionCancelled(interaction);
        clearActiveAgentInteraction();
        addMessageToUI({ role: 'assistant', content: `已结束与 ${agentName} 的本次多轮对话。` });
    } catch (error) {
        if (elements.agentInteractionStatusHint) {
            elements.agentInteractionStatusHint.textContent = `结束失败：${normalizeUserFacingErrorText(error.message)}`;
        }
        addErrorMessage(`结束多轮对话失败：${error.message || '请稍后重试'}`);
    } finally {
        state.isLoading = false;
        elements.agentInteractionEndBtn.disabled = false;
    }
}

// 流式聊天响应
async function streamChatResponse(text, typingId, imageUrl, attachmentIds = [], signal, streamContext = {}) {
    const payload = {
        message: text,
        conversation_id: streamContext.requestedConversationId || null,
        context: state.messages.slice(-10).map(m => ({ role: m.role, content: m.content }))
    };

    if (imageUrl) {
        payload.image_url = imageUrl;
    }
    if (attachmentIds.length > 0) {
        payload.attachment_ids = attachmentIds;
    }

    state.messages.push({ role: 'user', content: text });

    const response = await fetch(`${API_BASE}/chat/send`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getAuthHeaders()
        },
        body: JSON.stringify(payload),
        signal
    });

    if (!response.ok) {
        throw await createHttpError(response, '发送消息');
    }

    const resolvedConversationId = response.headers.get('X-Conversation-ID') || streamContext.requestedConversationId;
    if (resolvedConversationId) {
        const priorRuntimeKey = streamContext.runtimeKey;
        streamContext.runtimeKey = conversationRuntimeKey(resolvedConversationId);
        bindConversationRuntime(priorRuntimeKey, resolvedConversationId);
        const activate = String(state.currentConversationId || '') === String(streamContext.requestedConversationId || '');
        adoptConversationId(resolvedConversationId, { activate });
        if (activate) setConversationLoading(resolvedConversationId, true, getConversationRuntime(resolvedConversationId).controller);
        setConversationActivity(resolvedConversationId, 'running');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
            if (line.startsWith('data: ')) {
                const data = line.slice(6);
                if (data === '[DONE]') continue;

                try {
                    const eventData = JSON.parse(data);
                    await processEvent(eventData, typingId, streamContext);
                } catch (e) {
                    console.error('Failed to parse event:', e);
                }
            }
        }
    }
}

// 保存当前流式消息的状态
let currentStreamingMessage = null;
let currentStreamingMeta = null;

// 某些兜底路径会先发 agent_selection，再以 agent_stream/agent_reply 生成叮当回复。
// 选择面板必须跟随那条回复，而不是在收到事件时立即插到消息流末尾。
function createAgentSelectionRenderQueue() {
    const selectionsByConversation = new Map();
    return {
        enqueue(key, event) {
            const queueKey = String(key || '__active__');
            const selections = selectionsByConversation.get(queueKey) || [];
            selections.push(event);
            selectionsByConversation.set(queueKey, selections);
        },
        take(key) {
            const queueKey = String(key || '__active__');
            const selections = selectionsByConversation.get(queueKey) || [];
            selectionsByConversation.delete(queueKey);
            return selections;
        },
        clear(key) {
            selectionsByConversation.delete(String(key || '__active__'));
        }
    };
}

function getAgentSelectionRenderQueueKey(event = {}, streamContext = {}) {
    return event.conversation_id || streamContext.runtimeKey || state.currentConversationId || '__active__';
}

const agentSelectionRenderQueue = createAgentSelectionRenderQueue();

function renderAgentSelectionEvent(event, cardId) {
    const discoveryWarning = (event.discovery_warnings || []).map(item => item.message).filter(Boolean).join(' ');
    addAgentSelectionPanel(
        event.web_agents || [],
        event.ioa_agents || [],
        discoveryWarning || event.message,
        event.conversation_id,
        normalizeRecommendationEnvelope(event)
    );
    setInlineExecutionStatus(discoveryWarning || '已找到可用智能体，请选择并确认调用。', discoveryWarning ? 'attention' : 'completed');

    if (cardId) {
        const webCount = event.web_agents ? event.web_agents.length : 0;
        const ioaCount = event.ioa_agents ? event.ioa_agents.length : 0;
        const totalCount = webCount + ioaCount;
        const detail = `
            <div>共发现 <strong>${totalCount}</strong> 个智能体</div>
            ${discoveryWarning ? `<div role="status">${escapeHtml(discoveryWarning)}</div>` : ''}
            <div style="margin-top: 0.375rem;">${formatAgentTags(event.web_agents, event.ioa_agents)}</div>
        `;
        updateProcessStage(cardId, 1, 'completed', { detail });
        if (state.currentProcessCard) {
            state.currentProcessCard.conversationId = event.conversation_id;
        }
    }
}

function flushPendingAgentSelections(event, streamContext, cardId) {
    const queueKey = getAgentSelectionRenderQueueKey(event, streamContext);
    agentSelectionRenderQueue.take(queueKey).forEach(selection => {
        renderAgentSelectionEvent(selection, cardId);
    });
}

// 将当前流式助手回复写入 state.messages；仅在完成时调用一次，靠 saved 标记去重。
function saveCurrentStreamingMessageToState() {
    if (currentStreamingMessage && !currentStreamingMessage.saved && currentStreamingMessage.content) {
        state.messages.push({ role: 'assistant', content: currentStreamingMessage.content });
        currentStreamingMessage.saved = true;
    }
}

// 执行计时器
let executionStartTime = null;
let statusUpdateInterval = null;
let agentPollCounts = {};
let agentElapsedTimes = {};

// 处理 SSE 事件
function activityFromExecutionEvent(event) {
    const eventState = String(event?.state || event?.status || '').toLowerCase();
    if (eventState.includes('awaiting') || eventState.includes('waiting')) return 'attention';
    if (['completed', 'failed', 'rejected', 'canceled', 'cancelled'].includes(eventState)) return 'attention';
    if (event?.type === 'agent_result_card' && eventState) return 'running';
    if (event?.type === 'agent_reply' || event?.type === 'execution_complete' || event?.type === 'error') return 'attention';
    return 'running';
}

async function processEvent(event, typingId, streamContext = {}) {
    const eventConversationId = event.conversation_id || streamContext.runtimeKey;
    if (event.type === 'conversation_title') {
        updateConversationTitle(eventConversationId, event.title);
        return;
    }
    if (eventConversationId) {
        setConversationActivity(eventConversationId, activityFromExecutionEvent(event));
        if (String(eventConversationId) !== String(state.currentConversationId)) return;
    }
    const cardId = state.currentProcessCard?.id;

    if (event.type === 'searching') {
        setInlineExecutionStatus(event.message || '正在搜索智能体、安排任务…', 'running');
        const el = document.getElementById(typingId);
        if (el) {
            el.querySelector('.typing-text').textContent = event.message || '正在搜索智能体、安排任务...';
        }
        if (cardId) {
            updateProcessStage(cardId, 0, 'completed', {
                detail: event.message || '意图识别完成'
            });
            updateProcessStage(cardId, 1, 'running', {
                detail: event.message || '正在搜索匹配的智能体...'
            });
        }
    } else if (event.type === 'agent_stream') {
        if (!currentStreamingMessage) {
            removeTypingIndicator(typingId);
            const id = 'streaming-' + Date.now();
            currentStreamingMessage = { id, content: '' };
            currentStreamingMeta = {};
            const html = `
                <div id="${id}" class="message assistant">
                    <div class="message-avatar">
                        <img src="dingdang.png" alt="叮当">
                    </div>
                    <div class="message-content-wrapper">
                        <div class="message-content"></div>
                        ${renderCopyButton('')}
                    </div>
                </div>
            `;
            elements.messagesContainer.insertAdjacentHTML('beforeend', html);
        }
        
        currentStreamingMessage.content += event.content;
        if (event.llm_time !== undefined) currentStreamingMeta.llm_time = event.llm_time;
        if (event.ds_time !== undefined) currentStreamingMeta.ds_time = event.ds_time;
        
        const el = document.getElementById(currentStreamingMessage.id);
        if (el) {
            el.querySelector('.message-content').innerHTML = renderMarkdown(currentStreamingMessage.content);
            replaceDynamicActionHTML(el.querySelector('.message-content-wrapper'), `
                <div class="message-content">${renderMarkdown(currentStreamingMessage.content)}</div>
                ${renderCopyButton(currentStreamingMessage.content)}
                ${renderMessageMeta(currentStreamingMeta)}
            `);
        }
        scrollToBottom();
    } else if (event.type === 'llm_complete') {
        if (currentStreamingMessage) {
            const el = document.getElementById(currentStreamingMessage.id);
            if (el) {
                currentStreamingMeta.llm_time = event.llm_time;
                replaceDynamicActionHTML(el.querySelector('.message-content-wrapper'), `
                    <div class="message-content">${renderChatJsonSummaries(currentStreamingMessage.content, event.json_summaries)}</div>
                    ${renderCopyButton(currentStreamingMessage.content)}
                    ${renderMessageMeta(currentStreamingMeta)}
                `);
            }
            saveCurrentStreamingMessageToState();
        }
        if (cardId) {
            const card = state.currentProcessCard;
            if (card && card.stages[2].status === 'completed') {
                updateProcessStage(cardId, 3, 'completed', { detail: '回复已完成' });
            }
        }
    } else if (event.type === 'agent_selection') {
        removeTypingIndicator(typingId);

        // 正常路径是 assistant 文本已完成后再到选择事件；兜底路径恰好相反。
        // 后者先缓存，等本轮 agent_reply 将文字落入消息流后再呈现卡片。
        if (event.defer_selection_until_authoritative_reply === true) {
            agentSelectionRenderQueue.enqueue(getAgentSelectionRenderQueueKey(event, streamContext), event);
            // Discovery has already completed here.  The card deliberately
            // waits for Dingdang's authoritative explanation so prose appears
            // first; label that remaining work accurately instead of implying
            // that the external search is still slow.
            setInlineExecutionStatus('已找到可用智能体；叮当正在生成说明，候选卡随后显示。', 'running');
            return;
        }

        // The normal selectable-agent stream terminates at agent_selection;
        // it intentionally has no follow-up agent_reply.  Render the result
        // immediately when there is no Dingdang prose to anchor it to.
        if (!currentStreamingMessage || !String(currentStreamingMessage.content || '').trim()) {
            renderAgentSelectionEvent(event, cardId);
            return;
        }
        
        const el = document.getElementById(currentStreamingMessage.id);
        if (el) {
            const meta = { llm_time: event.llm_time, ds_time: event.ds_time };
            replaceDynamicActionHTML(el.querySelector('.message-content-wrapper'), `
                <div class="message-content">${renderRichMessage(currentStreamingMessage.content)}</div>
                ${renderCopyButton(currentStreamingMessage.content)}
                ${renderMessageMeta(meta)}
            `);
        }

        renderAgentSelectionEvent(event, cardId);
        saveCurrentStreamingMessageToState();
        currentStreamingMessage = null;
        currentStreamingMeta = null;
    } else if (event.type === 'agent_reply') {
        removeTypingIndicator(typingId);
        if (currentStreamingMessage && Array.isArray(event.json_summaries)) {
            const el = document.getElementById(currentStreamingMessage.id);
            if (el) {
                replaceDynamicActionHTML(el.querySelector('.message-content-wrapper'), `
                    <div class="message-content">${renderChatJsonSummaries(currentStreamingMessage.content, event.json_summaries)}</div>
                    ${renderCopyButton(currentStreamingMessage.content)}
                    ${renderMessageMeta(currentStreamingMeta || {})}
                `);
            }
        }
        saveCurrentStreamingMessageToState();
        currentStreamingMessage = null;
        currentStreamingMeta = null;
        clearInlineExecutionStatus();
        flushPendingAgentSelections(event, streamContext, cardId);
        
        if (cardId) {
            updateProcessStage(cardId, 3, 'completed', {
                detail: '已收到智能体回复'
            });
        }
    } else if (event.type === 'error') {
        removeTypingIndicator(typingId);
        addErrorMessage(event.message || '发生错误');
        clearInlineExecutionStatus();
        currentStreamingMessage = null;
        currentStreamingMeta = null;
        agentSelectionRenderQueue.clear(getAgentSelectionRenderQueueKey(event, streamContext));

        if (cardId) {
            const card = state.currentProcessCard;
            if (card) {
                let errorStage = 0;
                for (let i = card.stages.length - 1; i >= 0; i--) {
                    if (card.stages[i].status === 'running') {
                        errorStage = i;
                        break;
                    }
                    if (card.stages[i].status === 'completed' && i < card.stages.length - 1) {
                        errorStage = i + 1;
                        break;
                    }
                }
                updateProcessStage(cardId, errorStage, 'error', {
                    detail: event.message || '发生错误'
                });
            }
        }
    }
}

// 添加消息到 UI
function addMessageToUI(msg) {
    if (elements.messagesContainer.querySelector('.welcome-message')) {
        clearDynamicActionElement(elements.messagesContainer);
    }

    const html = createMessageHTML(msg);
    elements.messagesContainer.insertAdjacentHTML('beforeend', html);
    scrollToBottom();
}

// 添加打字指示器
function addTypingIndicator() {
    const id = 'typing-' + Date.now();
    const html = `
        <div id="${id}" class="message assistant">
            <div class="message-avatar">
                <img src="dingdang.png" alt="叮当">
            </div>
            <div class="message-content">
                <div class="typing-indicator">
                    <span></span>
                    <span></span>
                    <span></span>
                </div>
                <div class="typing-text is-flowing">正在思考...</div>
            </div>
        </div>
    `;
    if (elements.messagesContainer.querySelector('.welcome-message')) {
        clearDynamicActionElement(elements.messagesContainer);
    }
    elements.messagesContainer.insertAdjacentHTML('beforeend', html);
    scrollToBottom();
    return id;
}

// 移除打字指示器
function removeTypingIndicator(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

// 添加智能体选择面板（双栏布局）
function renderInlineAgentDiscovery(webAgents, ioaAgents, message, conversationId, recommendation = null) {
    // “随意探索”只返回智能体列表，不经过聊天 SSE，因此没有服务端
    // conversation_id。为这类确认卡创建可追踪的会话，确保后续“执行调用”
    // 能正常进入 AIP/SSE 调用链。
    const resolvedConversationId = conversationId || ensureConversationId('explore');
    const panel = registerAgentDiscoveryPanel(webAgents, ioaAgents, resolvedConversationId, recommendation);
    const html = createAgentSelectionHTML(panel, message);

    elements.messagesContainer.insertAdjacentHTML('beforeend', html);
    setupAgentCheckboxListeners(document.getElementById(panel.id));
    scrollToBottom();
    return html;
}

// 保留旧入口供历史消息恢复和嵌入调用复用；呈现统一落在消息流内。
function addAgentSelectionPanel(webAgents, ioaAgents, message, conversationId, recommendation = null) {
    return renderInlineAgentDiscovery(webAgents, ioaAgents, message, conversationId, recommendation);
}

// 切换 IOA 智能体选择状态
function toggleIoaAgent(agentName, panelId = '') {
    const panel = getAgentDiscoveryPanel(panelId);
    const selectedNames = panel?.selectedIoaAgents || state.selectedIoaAgents;
    if (selectedNames.has(agentName)) {
        selectedNames.delete(agentName);
    } else {
        selectedNames.add(agentName);
    }
    updateExecuteButton(panel?.id || panelId);
}

// 更新执行按钮状态
function updateExecuteButton(panelId = '') {
    const panel = getAgentDiscoveryPanel(panelId);
    const panelElement = panel ? document.getElementById(panel.id) : null;
    const btn = panelElement?.querySelector('.btn-execute-agents')
        || document.querySelector('.btn-execute-agents');
    if (btn) {
        btn.textContent = `执行已选智能体 (${panel?.selectedIoaAgents.size ?? state.selectedIoaAgents.size})`;
    }
}

function confirmRecommendedAgents(panelId = '') {
    const panel = getAgentDiscoveryPanel(panelId);
    if (!panel) {
        addErrorMessage('推荐组合已失效，请重新发起智能体发现。');
        return;
    }
    activateAgentDiscoveryPanel(panel);
    const recommendedEntries = getRecommendedEntries(panel, true);
    if (recommendedEntries.length === 0) {
        addErrorMessage('当前推荐组合没有可执行的 IOA 智能体，请手动选择候选。');
        return;
    }

    panel.selectedIoaAgents.clear();
    const taskByAgentName = new Map();
    recommendedEntries.forEach(entry => {
        const agentName = entry.agent?.name || entry.agentName;
        if (!agentName) return;
        panel.selectedIoaAgents.add(agentName);
        taskByAgentName.set(agentName, entry.assignedTask || '');
        const agent = panel.ioaAgents.find(item => item.name === agentName);
        if (agent && entry.assignedTask) agent.assigned_task = entry.assignedTask;
    });
    const panelElement = document.getElementById(panel.id);
    panelElement?.querySelectorAll('.agent-assigned-task-input').forEach(editor => {
        if (taskByAgentName.has(editor.dataset.agentName)) {
            editor.value = taskByAgentName.get(editor.dataset.agentName);
        }
    });
    updateExecuteButton(panel.id);
    executeSelectedAgents(null, {
        panelId: panel.id,
        recommendationConfirmed: true
    });
}

// 从已选 Agent 中挑出最适合当前应用模板的一个，用于预启动全屏 App。
function selectPreopenAgentForPreset(selectedAgents, preset) {
    const candidates = Array.isArray(selectedAgents) ? selectedAgents : [];
    if (!preset || candidates.length === 0) return null;

    const matchedIndex = candidates.findIndex(agent => {
        const capabilityText = getAgentCapabilityText(agent);
        const identityPreset = resolveAgentAppPresetByIdentity({
            agent_name: agent?.name,
            agent: agent?.name,
            endpoint: agent?.endpoint || agent?.url,
            aic: agent?.aic,
            skill_id: agent?.skill_id || agent?.skillId
        }, capabilityText);
        const intentPreset = detectAgentAppIntentPreset(capabilityText);
        return identityPreset?.kind === preset.kind || intentPreset?.kind === preset.kind;
    });

    const index = matchedIndex >= 0 ? matchedIndex : 0;
    return { agent: candidates[index], index };
}

// 根据已选 Agent 先打开本地 Agent App 占位页，避免定位或网络等待期间停留在聊天面板。
function preopenAgentAppForSelection(selectedAgents, preset, userQuery) {
    if (!state.realtimeAgentCardRendering) {
        return;
    }
    if (!preset || !selectedAgents || selectedAgents.length === 0 || !state.currentConversationId) {
        return;
    }

    const target = selectPreopenAgentForPreset(selectedAgents, preset);
    if (!target?.agent) return;

    const agent = target.agent;
    const cardId = `agent-card-${state.currentConversationId}-${target.index}`;
    const assignedTask = agent.assigned_task || userQuery || state.currentTaskIntent.queryText || `调用 ${agent.name || '智能体'}`;
    const launchEvent = {
        type: 'agent_result_card',
        operation: 'upsert',
        conversation_id: state.currentConversationId,
        card_id: cardId,
        agent: agent.name || '未知',
        agent_name: agent.name || '未知',
        aic: agent.aic || '',
        endpoint: agent.endpoint || agent.url || '',
        assigned_task: assignedTask,
        task_id: '',
        state: 'working',
        status: 'running',
        content: `正在通过 ACPs/AIP 调用 ${agent.name || '智能体'}...\n\n- 委托任务：${assignedTask}`,
        products: [],
        statusDataItems: [],
        raw_task_result: {},
        poll_count: 0,
        elapsed_time: 0,
        user_query_context: userQuery || state.currentTaskIntent.queryText || '',
        render_hints: {
            format: 'a2ui',
            renderer: 'mc',
            card_kind: preset.kind || 'generic',
            template_id: preset.id,
            blocks: ['a2ui', 'markdown', 'json-chart'],
            updatable: true
        }
    };

    if (!shouldOpenLocalAgentApp(launchEvent, preset, launchEvent.content)) {
        return;
    }

    openAgentApp(launchEvent, preset);
}

function setComposerTaskRunning(isRunning) {
    if (!elements.sendBtn) return;
    elements.sendBtn.disabled = false;
    elements.sendBtn.classList.toggle('sending', isRunning);
    elements.sendBtn.classList.toggle('is-stopping', isRunning);
    elements.sendBtn.setAttribute('aria-label', isRunning ? '结束当前对话及任务' : '发送消息');
    elements.sendBtn.innerHTML = isRunning
        ? '<span aria-hidden="true">■</span>'
        : '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 7-7 7 7M12 19V5"/></svg>';
}

async function endCurrentConversationTask() {
    if (!state.isLoading) return sendMessage();
    const controller = state.activeRequestController;
    const interaction = state.activeExecutionInteraction || state.activeAgentInteraction;
    if (interaction?.interaction_kind === 'collaboration' || interaction?.task_id || interaction?.taskId) {
        try {
            // Keep the SSE alive until the server accepts the cancellation request.
            await cancelAgentInteraction(interaction);
        } catch (error) {
            console.warn('Unable to cancel active agent task:', error);
            addErrorMessage(`停止显示，但未能确认智能体任务已取消：${normalizeUserFacingErrorText(error.message)}`);
            return;
        }
    }
    controller?.abort();
    clearInlineExecutionStatus();
    clearActiveAgentInteraction();
    state.activeExecutionInteraction = null;
    setConversationLoading(state.currentConversationId, false);
    addMessageToUI({ role: 'assistant', content: '已结束当前对话与正在执行的任务。' });
}

// 执行选中的 IOA 智能体
async function executeSelectedAgents(btnElement, options = {}) {
    const panelId = options.panelId
        || btnElement?.closest?.('.inline-agent-discovery')?.dataset.discoveryId
        || state.activeAgentDiscoveryPanelId;
    const panel = getAgentDiscoveryPanel(panelId);
    if (panel) activateAgentDiscoveryPanel(panel);
    const selectedNames = panel?.selectedIoaAgents || state.selectedIoaAgents;
    const ioaAgents = panel?.ioaAgents || state.currentIoaAgents;
    const conversationId = panel?.conversationId || state.currentConversationId;
    const panelElement = panel ? document.getElementById(panel.id) : null;

    if (selectedNames.size === 0) {
        alert('请至少选择一个智能体');
        return;
    }

    if (!conversationId) {
        alert('会话信息丢失，请重新发起请求');
        return;
    }

    const btn = btnElement
        || panelElement?.querySelector('.btn-execute-agents')
        || document.querySelector('.btn-execute-agents');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '执行中...';
    }

    const taskEditors = panelElement?.querySelectorAll('.agent-assigned-task-input')
        || document.querySelectorAll('.agent-assigned-task-input');
    const editedTasks = new Map(Array.from(taskEditors).map(input => [
        input.dataset.agentName,
        input.value.trim()
    ]));
    const invocationAgents = ioaAgents.map(agent => ({
        ...agent,
        assigned_task: editedTasks.has(agent.name)
            ? editedTasks.get(agent.name)
            : (agent.assigned_task || '')
    }));
    const selectedAgents = invocationAgents.filter(agent => selectedNames.has(agent.name));
    state.currentTaskIntent.weather = state.currentTaskIntent.weather || detectWeatherIntentFromAgents(selectedAgents);

    const requestController = new AbortController();
    setConversationLoading(conversationId, true, requestController);
    try {
        const clientContext = await buildClientContext({
            requireLocation: state.currentTaskIntent.weather
        });
        const response = await fetch(`${API_BASE}/agent/execute`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...getAuthHeaders()
            },
            body: JSON.stringify({
                conversation_id: conversationId,
                selected_agents: Array.from(selectedNames),
                user_query: options.userQuery || panel?.queryText || state.currentTaskIntent.queryText || '',
                ioa_agents: invocationAgents,
                client_context: clientContext,
                attachment_ids: panel?.attachmentIds || state.currentTaskIntent.attachmentIds || []
            }),
            signal: requestController.signal
        });

        if (!response.ok) {
            throw await createHttpError(response, '执行智能体调用');
        }

        applyQuotaHeaders(response);
        await processExecutionStream(response, conversationId);
    } catch (e) {
        if (e.name === 'AbortError') return;
        console.error('Error executing agents:', e);
        markCurrentProcessCardError(e.message || '执行智能体调用失败');
        updateExecutionProgress(`执行出错：${e.message}`);
    } finally {
        if (getConversationRuntime(conversationId).activity === 'running') {
            setConversationActivity(conversationId, 'attention');
        }
        setConversationLoading(conversationId, false);
        const currentBtn = btnElement
            || panelElement?.querySelector('.btn-execute-agents')
            || document.querySelector('.btn-execute-agents');
        if (currentBtn) {
            currentBtn.disabled = false;
            currentBtn.textContent = `执行已选智能体 (${selectedNames.size})`;
        }
    }
}

// 处理执行流
async function processExecutionStream(response, fallbackConversationId = null) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let terminalEvent = null;

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
            if (line.startsWith('data: ')) {
                const data = line.slice(6);
                if (data === '[DONE]') continue;

                try {
                    const eventData = JSON.parse(data);
                    await handleExecutionEvent(eventData, fallbackConversationId);
                    if (eventData.type === 'execution_complete' || eventData.type === 'execution_error' || eventData.type === 'execution_waiting_input' || eventData.type === 'execution_awaiting_completion') {
                        terminalEvent = eventData;
                    }
                } catch (e) {
                    console.error('Failed to parse execution event:', e);
                }
            }
        }
    }
    return terminalEvent;
}

function isNonCompletedExecutionComplete(event) {
    const protocolState = String(
        event.protocol_state || event.protocolState || event.state || ''
    ).trim().toLowerCase();
    return Boolean(protocolState) && protocolState !== 'completed';
}

// A run_id is no longer used as a source detector.  Single-member runs also
// receive one, and legacy direct-invoke events may have none.  The explicit
// envelope is the contract for the unified Dingdang collaboration flow.
function isCollaborationCompletionEvent(event) {
    return String(event?.execution_origin || '') === 'dingdang-collaboration'
        && String(event?.interaction_kind || '') === 'collaboration'
        && Boolean(String(event?.run_id || '').trim());
}

function renderExternalAgentCompletionPrompt(event) {
    state.pendingAgentCompletion = event;
    const confirmAction = renderDynamicActionAttributes('confirm-agent-completion', {});
    const continueAction = renderDynamicActionAttributes('continue-agent-completion', {});
    const agentName = event.agent_name || event.agent || '智能体';
    const candidateContent = event.content
        ? `<section class="secretary-completion-body" aria-label="候选结果"><div class="secretary-completion-section-title"><span aria-hidden="true">✦</span> 候选结果</div><div class="secretary-completion-candidate">${renderRichMessage(event.content)}</div></section>`
        : '';
    const prompt = document.createElement('div');
    prompt.className = 'message assistant agent-completion-prompt';
    prompt.innerHTML = `<div class="message-avatar"><img src="dingdang.png" alt="叮当"></div><div class="message-content-wrapper"><div class="message-content"><article class="secretary-completion-card"><header class="secretary-completion-header"><div><div class="secretary-completion-eyebrow"><span class="secretary-completion-pulse" aria-hidden="true"></span>${escapeHTML(agentName)} 协作</div><h3>候选结果等待确认</h3></div><span class="secretary-completion-status is-flowing">待确认</span></header><div class="secretary-completion-summary">${escapeHTML(event.message || `${agentName} 已生成候选结果，请确认或补充。`)}</div>${candidateContent}<footer class="secretary-completion-footer"><span class="secretary-completion-hint">补充内容将继续当前智能体任务。</span><div class="secretary-completion-actions"><button type="button" class="btn-primary" ${confirmAction}>确认当前结果</button><button type="button" class="btn-secondary" ${continueAction}>继续补充</button></div></footer></article></div></div>`;
    elements.messagesContainer.appendChild(prompt);
    scrollToBottom();
}

async function confirmExternalAgentCompletion() {
    const pending = state.pendingAgentCompletion;
    if (!pending || state.isLoading) return;
    if (!pending.task_id && !pending.taskId) {
        addErrorMessage('当前结果缺少 AIP task_id，无法确认完成。');
        return;
    }
    state.isLoading = true;
    try {
        const response = await fetch(`${API_BASE}/agent/complete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({
                task_id: pending.task_id || pending.taskId,
                agent: pending.agent_name || pending.agent || '智能体',
                conversation_id: pending.conversation_id || state.currentConversationId
            })
        });
        if (!response.ok) throw await createHttpError(response, '确认智能体结果');
        const completionMessage = {
            role: 'assistant',
            content: `已确认 ${pending.agent_name || pending.agent || '智能体'} 的结果完成。\n\n${pending.content || ''}`
        };
        state.pendingAgentCompletion = null;
        document.querySelectorAll('.agent-completion-prompt').forEach(prompt => prompt.remove());
        clearInlineExecutionStatus();
        clearActiveAgentInteraction();
        state.messages.push(completionMessage);
        addMessageToUI(completionMessage);
        setConversationActivity(state.currentConversationId, 'idle');
    } catch (error) {
        addErrorMessage(`确认智能体结果失败：${error.message || '请稍后重试'}`);
    } finally {
        state.isLoading = false;
    }
}

function continueExternalAgentCompletion() {
    const pending = state.pendingAgentCompletion;
    if (!pending) return;
    state.pendingAgentCompletion = null;
    document.querySelectorAll('.agent-completion-prompt').forEach(prompt => prompt.remove());
    const interaction = {
        ...pending,
        state: 'awaiting-input',
        status: 'awaiting-input',
        cancelable: true,
    };
    setActiveAgentInteraction(interaction);
    addMessageToUI({ role: 'assistant', content: `好的，请补充你希望调整的内容后继续 ${pending.agent_name || pending.agent || '智能体'}。` });
}

function updateAgentStatus(agentName, status, pollCount, elapsedTime) {
    const resolvedPollCount = pollCount ?? agentPollCounts[agentName] ?? 0;
    const resolvedElapsedTime = elapsedTime ?? agentElapsedTimes[agentName] ?? 0;
    agentPollCounts[agentName] = resolvedPollCount;
    agentElapsedTimes[agentName] = resolvedElapsedTime;
    
    const panelContent = document.querySelector('.status-panel .panel-content');
    if (!panelContent) return;
    
    const statusMap = {
        'running': { text: '调用中', class: 'status-running', icon: '⏳' },
        'success': { text: '成功', class: 'status-success', icon: '✅' },
        'error': { text: '失败', class: 'status-error', icon: '❌' },
        'awaiting-input': { text: '待输入', class: 'status-running', icon: '⏳' },
        'accepted': { text: '已接受', class: 'status-running', icon: '📋' },
        'working': { text: '工作中', class: 'status-running', icon: '⚙️' },
        'rejected': { text: '已拒绝', class: 'status-error', icon: '❌' },
        'canceled': { text: '已结束', class: 'status-cancelled', icon: '■' },
        'cancelled': { text: '已结束', class: 'status-cancelled', icon: '■' }
    };
    
    const statusInfo = statusMap[status] || statusMap['running'];
    
    let statusContainer = document.getElementById('agentStatusContainer');
    if (!statusContainer) {
        statusContainer = document.createElement('div');
        statusContainer.id = 'agentStatusContainer';
        panelContent.innerHTML = '';
        panelContent.appendChild(statusContainer);
    }
    
    let agentItem = document.getElementById(`agent-status-${agentName.replace(/\s+/g, '-')}`);
    if (!agentItem) {
        agentItem = document.createElement('div');
        agentItem.id = `agent-status-${agentName.replace(/\s+/g, '-')}`;
        agentItem.className = 'agent-status-item';
        statusContainer.appendChild(agentItem);
    }
    
    agentItem.className = `agent-status-item ${statusInfo.class}`;
    agentItem.innerHTML = `
        <div class="status-header">
            <span class="status-icon">${statusInfo.icon}</span>
            <span class="status-agent-name">${escapeHTML(agentName)}</span>
        </div>
        <div class="status-details">
            <span class="status-text">${statusInfo.text}</span>
            <span class="status-metric status-poll-count">轮询: ${resolvedPollCount}次</span>
            <span class="status-metric status-elapsed-time">用时: ${resolvedElapsedTime}s</span>
        </div>
    `;
}

function finalizeRunningAgentStatuses() {
    const statusContainer = document.getElementById('agentStatusContainer');
    if (!statusContainer) return;
    const runningNames = [...statusContainer.querySelectorAll('.agent-status-item.status-running')]
        .map(item => item.querySelector('.status-agent-name')?.textContent)
        .filter(Boolean);
    runningNames.forEach(agentName => {
        updateAgentStatus(agentName, 'success', agentPollCounts[agentName], agentElapsedTimes[agentName]);
    });
}

function updateExecutionTimes() {
    if (!executionStartTime) return;
    
    const elapsedSeconds = Math.floor((Date.now() - executionStartTime) / 1000);
    const statusContainer = document.getElementById('agentStatusContainer');
    if (!statusContainer) return;
    
    const runningAgents = statusContainer.querySelectorAll('.status-running');
    runningAgents.forEach(agentItem => {
        const agentName = agentItem.querySelector('.status-agent-name')?.textContent;
        // 服务端与适配器均按 2 秒轮询。执行中按相同节奏更新；
        // 收到服务端的真实 poll_count 后，再由 updateAgentStatus 覆盖。
        const pollCount = Math.max(agentPollCounts[agentName] || 0, Math.floor(elapsedSeconds / 2));
        agentPollCounts[agentName] = pollCount;
        agentElapsedTimes[agentName] = elapsedSeconds;
        const detailsEl = agentItem.querySelector('.status-details');
        if (detailsEl) {
            const existingTime = detailsEl.querySelector('.status-elapsed-time');
            const existingPoll = detailsEl.querySelector('.status-poll-count');
            if (existingTime) existingTime.textContent = `用时: ${elapsedSeconds}s`;
            else detailsEl.innerHTML += `<span class="status-metric status-elapsed-time">用时: ${elapsedSeconds}s</span>`;
            if (existingPoll) existingPoll.textContent = `轮询: ${pollCount}次`;
            else detailsEl.innerHTML += `<span class="status-metric status-poll-count">轮询: ${pollCount}次</span>`;
        }
    });
}

function openCollaborationTaskDetail(task) {
    document.querySelector('.collaboration-detail-modal')?.remove();
    const evidence = task.llmEvidence || {};
    const trace = [
        task.agentAic ? `AIC：${task.agentAic}` : '',
        task.aipTaskId ? `AIP 任务：${task.aipTaskId}` : '',
        evidence.transport ? `链路：${evidence.transport}` : '',
        evidence.delivery_state ? `提交状态：${evidence.delivery_state}` : '',
        evidence.protocol_state ? `协议状态：${evidence.protocol_state}` : '',
        evidence.terminal_state ? `终态：${evidence.terminal_state}` : '',
        Number.isFinite(evidence.elapsed_time) ? `实际用时：${evidence.elapsed_time}s` : '',
        Number.isFinite(evidence.poll_count) ? `轮询：${evidence.poll_count} 次` : '',
        evidence.assigned_instruction ? `输入指令：${evidence.assigned_instruction}` : '',
        evidence.partner_diagnostic ? `外部智能体诊断：${evidence.partner_diagnostic}` : ''
    ].filter(Boolean).map(escapeHTML).join('<br>');
    const isSecretary = task.kind === 'secretary' || task.id === 'secretary';
    const plan = task.artifact?.plan;
    const planView = isSecretary && plan?.valid ? `
        <section class="secretary-plan-card">
            <span class="collaboration-kicker">秘书编排决策</span>
            <div class="secretary-plan-grid">
                <div><small>协作方式</small><strong>${plan.mode === 'sequential' ? '串行交接' : '并行协作'}</strong></div>
                <div><small>对外发言人</small><strong>${escapeHTML(plan.speaker || '叮当汇总')}</strong></div>
                <div><small>用户补充</small><strong>${plan.await_user ? '需要补充信息' : '当前无需补充'}</strong></div>
            </div>
            <p>${escapeHTML(plan.brief || task.message || '已完成协作编排。')}</p>
            ${plan.question ? `<div class="secretary-plan-question">待补充：${escapeHTML(plan.question)}</div>` : ''}
        </section>` : `<div class="collaboration-markdown">${renderMarkdown(task.artifact?.summary || task.message || '该智能体尚未产生输出。')}</div>`;
    const modal = document.createElement('div');
    modal.className = 'collaboration-detail-modal';
    modal.innerHTML = `<div class="collaboration-detail-dialog glass-card" role="dialog" aria-modal="true" aria-label="${escapeHTMLAttribute(`${task.agent || '协作成员'} 详情`)}">
        <button type="button" class="collaboration-detail-close" aria-label="关闭">×</button>
        <span class="collaboration-kicker">实时协作详情</span><h3>${escapeHTML(isSecretary ? '叮当 · 多智能体协调' : task.agent || '协作成员')}</h3>
        ${!isSecretary ? `<section class="secretary-plan-card"><small>成员任务</small><p>${escapeHTML(task.title || '未记录成员任务。')}</p></section>` : ''}
        <div class="collaboration-trace">${trace || '尚未收到协议回执。'}</div>
        ${planView}
    </div>`;
    modal.addEventListener('click', event => { if (event.target === modal || event.target.closest('.collaboration-detail-close')) modal.remove(); });
    document.body.appendChild(modal);
}

function normalizeCollaborationStatus(status) {
    const normalized = String(status || '').toLowerCase();
    return new Set(['pending', 'running', 'completed', 'failed', 'awaiting-input', 'awaiting-completion'])
        .has(normalized) ? normalized : 'pending';
}

function renderCollaborationRun(run) {
    const messageFlow = elements.messagesContainer || document.getElementById('messagesContainer');
    if (!messageFlow || !run) return;
    if (messageFlow.querySelector('.welcome-message')) messageFlow.innerHTML = '';
    let root = Array.from(messageFlow.querySelectorAll('[data-collaboration-run]'))
        .find(item => item.dataset.collaborationRun === String(run.id));
    if (!root) {
        const message = document.createElement('div');
        message.className = 'message assistant collaboration-message';
        const avatar = document.createElement('div');
        avatar.className = 'message-avatar';
        avatar.innerHTML = '<img src="dingdang.png" alt="叮当">';
        const wrapper = document.createElement('div');
        wrapper.className = 'message-content-wrapper';
        root = document.createElement('section');
        root.className = 'collaboration-run-card collaboration-message-module';
        root.dataset.collaborationRun = String(run.id);
        wrapper.appendChild(root);
        message.appendChild(avatar);
        message.appendChild(wrapper);
        messageFlow.appendChild(message);
    }
    const done = run.tasks.filter(task => task.status === 'completed').length;
    const hasWaitingStatus = run.tasks.some(task => isFlowingStatus(task.status));
    replaceDynamicActionHTML(root, `
        <div class="collaboration-run-head">
            <div><span class="collaboration-kicker">协作驾驶舱</span><strong>${escapeHTML(run.title)}</strong></div>
            <span class="collaboration-count">${done}/${run.tasks.length}</span>
        </div>
        <p class="collaboration-goal">${escapeHTML(run.goal || '正在处理任务')}</p>
        <div class="collaboration-task-graph">
            ${run.tasks.map(task => {
                const status = normalizeCollaborationStatus(task.status);
                const icon = status === 'completed' ? '✓' : status === 'running' ? '◌' : status === 'failed' ? '!' : '·';
                const isSecretary = task.kind === 'secretary' || task.id === 'secretary';
                const displayTitle = isSecretary ? '叮当' : (task.agent || task.title);
                const displayRole = isSecretary ? '多智能体协调 · 编排与汇总' : '协作成员 · 执行任务';
                return `<button type="button" class="collaboration-node ${isSecretary ? 'secretary' : 'agent'} ${status}" data-collaboration-task="${escapeHTMLAttribute(task.id)}" title="${escapeHTMLAttribute(`${displayTitle}：${task.message || task.title || ''}`)}"><i>${icon}</i><span>${escapeHTML(displayTitle)}</span><small>${escapeHTML(displayRole)}</small></button>`;
            }).join('')}
        </div>
        ${renderCollaborationFlow(run)}
        ${run.tasks.some(task => task.result) ? `<div class="collaboration-subtask-results">${run.tasks.filter(task => task.result).map(task => `
            <section class="collaboration-subtask-result" data-collaboration-result="${escapeHTMLAttribute(task.id)}">
                <strong>${escapeHTML(task.agent || task.title || '协作成员')}</strong>
                <div class="collaboration-markdown">${renderRichMessage(task.result)}</div>
            </section>`).join('')}</div>` : ''}
        <div class="collaboration-detail ${hasWaitingStatus ? 'is-flowing' : ''}" aria-live="polite"><strong>叮当 · 协调进度</strong><br>${escapeHTML(run.detail || '正在协调协作。')}</div>`);
    root.querySelectorAll('[data-collaboration-task]').forEach(button => {
        button.addEventListener('click', () => {
            const task = run.tasks.find(item => item.id === button.dataset.collaborationTask);
            const detail = root.querySelector('.collaboration-detail');
            if (!task || !detail) return;
            openCollaborationTaskDetail(task);
        });
    });
    root.querySelector('[data-dag-fullscreen]')?.addEventListener('click', () => openCollaborationDagFullscreen(run));
    scrollToBottom();
}

function applySubtaskResultEvent(event) {
    const run = event.run_id ? state.collaborationRuns[event.run_id] : null;
    if (run) {
        let task = run.tasks.find(item => item.id === event.task_id);
        if (!task && event.task_id) {
            task = { id: event.task_id, agent: event.agent || '协作成员', title: event.task_id, status: 'pending', message: '' };
            run.tasks.push(task);
        }
        if (task) {
            task.status = event.status === 'success' ? 'completed' : (event.status || task.status);
            task.result = String(event.result || '');
            task.message = event.message || task.message || (event.status === 'success' ? '子任务已完成' : '子任务执行失败');
            run.detail = `${task.agent || task.title} · ${task.message}`;
        }
        renderCollaborationRun(run);
        return;
    }
    addStandardAgentResultMessage({
        ...event,
        card_id: event.task_id || `subtask-${Date.now()}`,
        agent_name: event.agent || '协作成员',
        state: event.status === 'success' ? 'completed' : 'failed',
        content: event.result || event.message || '子任务已结束。'
    }, { force: true });
}

function renderCollaborationFlow(run) {
    if (run.dag?.nodes?.length) return renderCollaborationDag(run);
    const tasksById = Object.fromEntries(run.tasks.map(task => [task.id, task]));
    const edges = run.edges || [];
    if (!edges.length) return `<div class="collaboration-flow is-planning"><span>数据流转图将在叮当完成协调后显示</span></div>`;
    return `<section class="collaboration-flow" aria-label="智能体协作数据流转">
        <div class="collaboration-flow-head"><strong>协作与数据流转</strong><span>${run.mode === 'sequential' ? '串行交接' : '并行协作'}</span></div>
        <div class="collaboration-flow-list">${edges.map(edge => {
            const from = tasksById[edge.from] || {};
            const to = tasksById[edge.to] || {};
            const active = from.status === 'running' || to.status === 'running';
            const complete = from.status === 'completed' && to.status === 'completed';
            const state = complete ? 'completed' : active ? 'running' : 'pending';
            const fromName = edge.from === 'secretary' ? '叮当' : (from.agent || from.title || edge.from);
            const toName = edge.to === 'secretary' ? '叮当' : (to.agent || to.title || edge.to);
            return `<div class="collaboration-flow-edge ${state}"><span>${escapeHTML(fromName)}</span><i>→</i><b>${escapeHTML(edge.label)}</b><i>→</i><span>${escapeHTML(toName)}</span></div>`;
        }).join('')}</div>
    </section>`;
}

function getDagFocusNodeIds(nodes, edges, statusFor) {
    const active = nodes.filter(node => statusFor(node.id) === 'running');
    if (!active.length) return new Set(nodes.map(node => node.id));
    const ids = new Set(active.map(node => node.id));
    edges.forEach(edge => {
        if (ids.has(edge.source_node_id)) ids.add(edge.target_node_id);
        if (ids.has(edge.target_node_id)) ids.add(edge.source_node_id);
    });
    return ids;
}

function truncateDagLabel(value, maxLength) {
    const characters = Array.from(String(value || ''));
    return characters.length > maxLength ? `${characters.slice(0, maxLength - 1).join('')}…` : characters.join('');
}

function getDagArtifactLabel(edge) {
    const artifactType = (edge.artifact_selector?.artifact_types || [])[0] || '';
    const labels = {
        'task.instruction': '任务指令',
        'generic.text': '文本结果',
        'proposal.draft': '候选方案',
        'review.feedback': '审校反馈',
        'calculation.result': '计算结果'
    };
    return labels[artifactType] || truncateDagLabel(artifactType || '数据流转', 8);
}

function getDagViewBox(fullWidth, fullHeight) {
    // A focused running node must not resize the SVG viewport: doing so also
    // scales its text above the ordinary reply text. Fullscreen navigation is
    // the explicit, user-controlled way to zoom the graph.
    return `0 0 ${fullWidth} ${fullHeight}`;
}

function renderCollaborationDag(run, options = {}) {
    const nodes = run.dag.nodes;
    const edges = run.dag.edges || [];
    const nodeById = Object.fromEntries(nodes.map(node => [node.id, node]));
    const levels = {};
    nodes.forEach(node => { levels[node.id] = 0; });
    for (let pass = 0; pass < nodes.length; pass++) edges.forEach(edge => {
        const from = edge.source_node_id;
        const to = edge.target_node_id;
        if (from in levels && to in levels) levels[to] = Math.max(levels[to], levels[from] + 1);
    });
    const rows = {};
    nodes.forEach(node => { const level = levels[node.id]; (rows[level] ||= []).push(node); });
    const fullscreen = Boolean(options.fullscreen);
    const nodeWidth = fullscreen ? 188 : 118;
    const nodeHeight = fullscreen ? 66 : 50;
    const levelGap = fullscreen ? 238 : 150;
    const rowGap = fullscreen ? 108 : 82;
    const startX = fullscreen ? 30 : 18;
    const maxRows = Math.max(...Object.values(rows).map(items => items.length));
    const width = Math.max(fullscreen ? 760 : 320, startX * 2 + (Math.max(...Object.values(levels)) * levelGap) + nodeWidth);
    const height = Math.max(fullscreen ? 420 : 130, (fullscreen ? 62 : 22) * 2 + maxRows * rowGap);
    const startY = fullscreen
        ? Math.max(62, Math.round((height - nodeHeight - (maxRows - 1) * rowGap) / 2))
        : 22;
    const positions = {};
    Object.entries(rows).forEach(([level, items]) => items.forEach((node, index) => {
        positions[node.id] = { x: startX + Number(level) * levelGap, y: startY + index * rowGap };
    }));
    const taskById = Object.fromEntries(run.tasks.map(task => [task.id, task]));
    const statusFor = id => normalizeCollaborationStatus(run.dagNodeStatuses?.[id] || taskById[id]?.status || (id === 'secretary' ? 'completed' : 'pending'));
    const focusIds = getDagFocusNodeIds(nodes, edges, statusFor);
    const viewBox = getDagViewBox(width, height);
    const isLive = nodes.some(node => statusFor(node.id) === 'running');
    return `<section class="collaboration-flow collaboration-dag" aria-label="智能体协作 DAG 数据流转">
        <div class="collaboration-flow-head"><strong>协作与数据流转</strong><div class="dag-head-actions"><span class="dag-version-chip">DAG v${escapeHTML(String(run.dagVersion || 1))}</span><span class="dag-live-state ${isLive ? 'is-live' : ''}">${isLive ? '实时执行中' : '协作状态'}</span>${fullscreen ? '<div class="dag-navigation-controls" aria-label="DAG 视图控制"><button type="button" data-dag-zoom="out" aria-label="缩小 DAG">−</button><output data-dag-zoom-level>100%</output><button type="button" data-dag-zoom="in" aria-label="放大 DAG">+</button><button type="button" data-dag-zoom="reset" aria-label="重置并适配 DAG">适配</button></div>' : '<button type="button" class="dag-fullscreen-btn" data-dag-fullscreen aria-label="全屏查看 DAG" title="全屏查看">⛶</button>'}</div></div>
        <div class="dag-version-note">${escapeHTML(run.dagReason || '叮当已依据任务依赖生成协作图。')}</div>
        <div class="dag-canvas ${isLive ? 'is-focused' : ''}" tabindex="0"><svg style="--dag-natural-width:${width}px" viewBox="${viewBox}" role="img" aria-label="多智能体协作有向无环图"><defs><marker id="dag-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" /></marker></defs>
        ${edges.map(edge => { const a = positions[edge.source_node_id]; const b = positions[edge.target_node_id]; if (!a || !b) return ''; const active = statusFor(edge.source_node_id) === 'running' || statusFor(edge.target_node_id) === 'running'; const centerY = a.y + nodeHeight / 2; const labelY = centerY - (fullscreen ? 13 : 8); return `<path class="dag-edge ${active ? 'dag-edge-active' : ''}" d="M${a.x + nodeWidth},${centerY} C${a.x + nodeWidth + 22},${centerY} ${b.x - 22},${b.y + nodeHeight / 2} ${b.x},${b.y + nodeHeight / 2}" marker-end="url(#dag-arrow)"/>${fullscreen ? `<text class="dag-edge-label" x="${(a.x + nodeWidth + b.x) / 2}" y="${labelY}">${escapeHTML(getDagArtifactLabel(edge))}</text>` : ''}`; }).join('')}
        ${nodes.map(node => { const p = positions[node.id]; const status = statusFor(node.id); const name = node.id === 'secretary' ? '叮当规划' : node.id === 'secretary_synthesis' ? '叮当汇总' : (node.agent_ref || node.id); const displayName = truncateDagLabel(name, fullscreen ? 18 : 11); const stateLabel = ({running:'执行中',completed:'已完成',failed:'失败',pending:'待执行','awaiting-input':'待补充','awaiting-completion':'待确认'})[status] || status; return `<g class="dag-node ${status} ${focusIds.has(node.id) ? 'dag-focus' : 'dag-muted'}" data-dag-node="${escapeHTMLAttribute(node.id)}"><title>${escapeHTML(name)} · ${escapeHTML(stateLabel)}</title><rect x="${p.x}" y="${p.y}" width="${nodeWidth}" height="${nodeHeight}" rx="${fullscreen ? 13 : 10}"/><text x="${p.x + (fullscreen ? 14 : 10)}" y="${p.y + (fullscreen ? 28 : 22)}">${escapeHTML(displayName)}</text><text class="dag-node-state" x="${p.x + (fullscreen ? 14 : 10)}" y="${p.y + (fullscreen ? 49 : 39)}">${escapeHTML(stateLabel)}</text></g>`; }).join('')}
        </svg></div>
        <ol class="sr-only">${nodes.map(node => `<li>${escapeHTML(node.agent_ref || node.id)}，${escapeHTML(statusFor(node.id))}</li>`).join('')}</ol>
    </section>`;
}

function openCollaborationDagFullscreen(run) {
    document.querySelector('.dag-fullscreen-modal')?.remove();
    const modal = document.createElement('div');
    modal.className = 'dag-fullscreen-modal';
    modal.innerHTML = `<div class="dag-fullscreen-dialog glass-card" role="dialog" aria-modal="true" aria-label="全屏协作 DAG"><button type="button" class="dag-fullscreen-close" aria-label="关闭全屏 DAG">×</button>${renderCollaborationDag(run, { fullscreen: true })}</div>`;
    modal.addEventListener('click', event => { if (event.target === modal || event.target.closest('.dag-fullscreen-close')) modal.remove(); });
    document.body.appendChild(modal);
    enableFullscreenDagNavigation(modal);
}

function enableFullscreenDagNavigation(modal) {
    const canvas = modal.querySelector('.dag-canvas');
    const svg = canvas?.querySelector('svg');
    const zoomLevel = modal.querySelector('[data-dag-zoom-level]');
    if (!canvas || !svg || !zoomLevel) return;
    let zoom = 1;
    let drag = null;
    const applyZoom = nextZoom => {
        zoom = Math.min(2.4, Math.max(.7, Number(nextZoom.toFixed(2))));
        svg.style.width = `${zoom * 100}%`;
        svg.style.minWidth = `${Math.round(47.5 * zoom * 1000) / 1000}rem`;
        zoomLevel.value = `${Math.round(zoom * 100)}%`;
    };
    modal.querySelectorAll('[data-dag-zoom]').forEach(button => button.addEventListener('click', () => {
        const action = button.dataset.dagZoom;
        applyZoom(action === 'in' ? zoom + .2 : action === 'out' ? zoom - .2 : 1);
        if (action === 'reset') {
            canvas.scrollTo({ left: 0, top: 0, behavior: 'smooth' });
        }
    }));
    canvas.addEventListener('wheel', event => {
        if (!event.ctrlKey && !event.metaKey) return;
        event.preventDefault();
        applyZoom(zoom + (event.deltaY < 0 ? .1 : -.1));
    }, { passive: false });
    canvas.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        drag = { x: event.clientX, y: event.clientY, left: canvas.scrollLeft, top: canvas.scrollTop };
        canvas.classList.add('is-panning');
        canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', event => {
        if (!drag) return;
        canvas.scrollLeft = drag.left - (event.clientX - drag.x);
        canvas.scrollTop = drag.top - (event.clientY - drag.y);
    });
    const stopDragging = event => {
        if (!drag) return;
        drag = null;
        canvas.classList.remove('is-panning');
        if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    };
    canvas.addEventListener('pointerup', stopDragging);
    canvas.addEventListener('pointercancel', stopDragging);
}

function applyCollaborationEvent(event) {
    const runId = event.run_id;
    if (event.type === 'collaboration_run') {
        state.collaborationRuns[runId] = {
            id: runId, title: event.title || '多智能体协作', goal: event.goal || '', detail: '已收到协作请求，等待首个真实 AIP/LLM 调用。',
            tasks: (event.tasks || []).map(task => ({ ...task, status: 'pending', message: '' }))
        };
    }
    const run = state.collaborationRuns[runId];
    if (!run) return;
    if (event.type === 'collaboration_task') {
        const task = run.tasks.find(item => item.id === event.task_id);
        if (task) {
            task.status = event.status || task.status;
            task.message = event.message || event.artifact?.summary || task.message;
            if (event.artifact) task.artifact = event.artifact;
            if (event.agent_aic) task.agentAic = event.agent_aic;
            if (event.aip_task_id) task.aipTaskId = event.aip_task_id;
            if (event.llm_evidence) task.llmEvidence = event.llm_evidence;
            run.detail = `${task.agent} · ${task.message || task.title}`;
        }
        if (!task && event.task_id && event.status) {
            run.dagNodeStatuses ||= {};
            run.dagNodeStatuses[event.task_id] = event.status;
            run.detail = event.message || run.detail;
        }
    }
    if (event.type === 'collaboration_review') {
        const task = run.tasks.find(item => item.id === event.task_id);
        if (task) task.message = event.message || task.message;
        run.detail = `审校结论：${event.message || '已完成复核'}`;
    }
    if (event.type === 'collaboration_summary') run.detail = event.message || '正在整合协作结果。';
    if (event.type === 'collaboration_plan') {
        run.mode = event.mode || 'parallel';
        run.edges = Array.isArray(event.edges) ? event.edges : [];
    }
    if (event.type === 'node_updated' && event.node_id && event.status) {
        run.dagNodeStatuses ||= {};
        run.dagNodeStatuses[event.node_id] = event.status;
    }
    if (event.type === 'dag_snapshot') {
        if (Number(event.event_seq || 0) <= Number(run.lastDagEventSeq || 0)) return;
        run.lastDagEventSeq = event.event_seq;
        run.dagVersion = event.dag_version;
        run.dagReason = event.reason;
        run.dag = { nodes: event.nodes || [], edges: event.edges || [] };
    }
    renderCollaborationRun(run);
    if (state.currentProcessCard?.kind === 'secretary-continuation') {
        const activity = event.type === 'collaboration_summary'
            ? event.message
            : event.type === 'collaboration_plan'
                ? `已确定${event.mode === 'sequential' ? '串行交接' : '并行协作'}流程。`
                : event.type === 'collaboration_task'
                    ? `${event.task_id === 'secretary' ? '叮当' : (event.agent || '协作成员')}：${event.message || '状态已更新。'}`
                    : '协作状态已更新。';
        appendSecretaryContinuationActivity(activity);
    }
}

function markCollaborationRunFailed(runId, message) {
    const run = state.collaborationRuns[runId];
    if (!run) return;
    run.status = 'failed';
    run.detail = message || '协作执行发生错误。';
    run.tasks.forEach(task => {
        if (task.status === 'running') task.status = 'failed';
    });
    renderCollaborationRun(run);
}

async function handleExecutionEvent(event, fallbackConversationId = null, options = {}) {
    const replay = options.replay === true;
    const eventConversationId = event.conversation_id || fallbackConversationId;
    if (event.type === 'conversation_title') {
        updateConversationTitle(eventConversationId, event.title);
        return;
    }
    if (eventConversationId && !replay) {
        setConversationActivity(eventConversationId, activityFromExecutionEvent(event));
        if (String(eventConversationId) !== String(state.currentConversationId)) return;
    }
    if (!replay) rememberActiveExecutionInteraction(event);
    const progressEl = document.getElementById('executionProgress');
    const cardId = state.currentProcessCard?.id;

    switch (event.type) {
        case 'collaboration_run':
        case 'collaboration_task':
        case 'collaboration_review':
        case 'collaboration_summary':
        case 'collaboration_plan':
        case 'dag_snapshot':
        case 'dag_patch':
        case 'artifact_ready':
        case 'node_updated':
            applyCollaborationEvent(event);
            break;
        case 'execution_start':
            state.activeExecutionAgentCount = Number(event.agent_count || 0);
            setInlineExecutionStatus(event.message || '正在执行智能体调用…', 'running');
            if (progressEl) {
                progressEl.innerHTML = `<div class="progress-item running">${event.message}</div>`;
            }
            const panelContent = document.querySelector('.status-panel .panel-content');
            if (panelContent) {
                panelContent.innerHTML = '<div id="agentStatusContainer"></div>';
            }
            executionStartTime = Date.now();
            if (statusUpdateInterval) clearInterval(statusUpdateInterval);
            statusUpdateInterval = setInterval(updateExecutionTimes, 1000);

            if (state.currentProcessCard?.kind === 'secretary-continuation') {
                setSecretaryContinuationState('working', event.message || '叮当正在恢复协作。');
            } else if (cardId) {
                updateProcessStage(cardId, 2, 'running', {
                    detail: event.message || '开始执行智能体调用...'
                });
            }
            break;

        case 'decomposition_progress':
            setInlineExecutionStatus(event.message || '正在拆分任务…', 'running');
            if (progressEl) {
                progressEl.innerHTML += `
                    <div class="progress-item running">
                        ⏳ ${event.message}
                    </div>
                `;
            }
            break;

        case 'workflow_stage': {
            const stageClass = event.status === 'blocked'
                ? 'error'
                : (event.status === 'completed' ? 'completed' : 'running');
            const stageIcon = event.status === 'blocked'
                ? '⛔'
                : (event.status === 'completed' ? '✅' : (event.status === 'discovering' ? '🔎' : '⏳'));
            setInlineExecutionStatus(
                event.message || `第 ${event.stage || ''} 阶段`,
                event.status === 'blocked' ? 'error' : (event.status === 'completed' ? 'completed' : 'running')
            );
            if (progressEl) {
                progressEl.innerHTML += `
                    <div class="progress-item ${stageClass}" data-workflow-stage="${escapeHTMLAttribute(String(event.stage || ''))}">
                        ${stageIcon} ${escapeHTML(event.message || `第 ${event.stage || ''} 阶段`)}
                    </div>
                `;
            }
            if (cardId) {
                const scopedAgent = event.agent
                    ? `${getAgentDeploymentScope(event).selectionLabel}：${event.agent}`
                    : '';
                updateProcessStage(cardId, 2, event.status === 'blocked' ? 'error' : 'running', {
                    detail: scopedAgent
                        ? `${escapeHTML(scopedAgent)}<br><small>${escapeHTML(event.message || '正在执行顺序工作流...')}</small>`
                        : (event.message || '正在执行顺序工作流...')
                });
            }
            break;
        }

        case 'workflow_discovery': {
            setInlineExecutionStatus(
                event.message || '正在发现智能体…',
                event.status === 'failed' ? 'error' : (event.status === 'completed' ? 'completed' : 'running')
            );
            if (progressEl) {
                const progressClass = event.status === 'failed' ? 'error' : (event.status === 'completed' ? 'completed' : 'running');
                const progressIcon = event.status === 'failed' ? '❌' : (event.status === 'completed' ? '✅' : '🔎');
                progressEl.innerHTML += `<div class="progress-item ${progressClass}">${progressIcon} ${escapeHTML(event.message || '正在发现智能体…')}</div>`;
            }
            if (cardId) {
                if (event.status === 'completed') {
                    updateProcessCardIdentity(cardId, `云端智能体：${event.agent || '目标智能体'}`);
                    updateProcessStage(cardId, 1, 'completed', {
                        detail: `云端智能体：${escapeHTML(event.agent || '目标智能体')}<br><small>AIC ${escapeHTML(event.aic || '')}</small>`
                    });
                    updateProcessStage(cardId, 2, 'running', {
                        detail: `已选定云端智能体：${escapeHTML(event.agent || '目标智能体')}，正在发起 ACPs Direct RPC…`
                    });
                } else if (event.status === 'failed') {
                    updateProcessStage(cardId, 1, 'error', {
                        detail: event.message || 'Discovery 失败'
                    });
                } else {
                    updateProcessStage(cardId, 1, 'running', {
                        detail: event.message || '正在通过 ACPs Discovery 查找目标智能体…'
                    });
                }
            }
            break;
        }

        case 'workflow_handoff':
            if (cardId) {
                updateProcessStage(cardId, 2, 'running', {
                    detail: event.message || '前序任务已完成，正在切换到下一阶段。'
                });
            }
            try {
                await handleWorkflowHandoff(event);
            } catch (error) {
                console.error('[Dingdang] 第二轮调用失败:', error);
                addMessageToUI({
                    role: 'assistant',
                    content: `第二轮调用失败：${error.message}`
                });
                if (cardId) {
                    updateProcessStage(cardId, 2, 'error', {
                        detail: error.message || '第二轮调用失败'
                    });
                }
            }
            break;

        case 'execution_progress':
            setInlineExecutionStatus(
                event.message || `${event.agent || '智能体'} 正在执行`,
                ['error', 'failed', 'rejected'].includes(event.status)
                    ? 'error'
                    : (event.status === 'success' ? 'completed' : 'running')
            );
            if (['running', 'accepted', 'working'].includes(event.status)) {
                updateAgentStatus(event.agent, event.status, event.poll_count || 0, event.elapsed_time || 0);
                if (progressEl) {
                    progressEl.innerHTML += `
                        <div class="progress-item running">
                            ⏳ ${event.message}
                        </div>
                    `;
                }
            } else {
                updateAgentStatus(event.agent, event.status, event.poll_count, event.elapsed_time);
                const isAwaitingInput = event.status === 'awaiting-input';
                let icon = event.status === 'success' ? '✅' : (isAwaitingInput ? '⌁' : '❌');
                let message = event.message;
                
                if (isAwaitingInput) {
                    message = `${event.agent} 等待补充输入`;
                } else if (event.status === 'error') {
                    const errorMsg = event.message || '';
                    if (errorMsg.includes('busy') || errorMsg.includes('繁忙')) {
                        icon = '⚠️';
                        message = `${event.agent} 暂时繁忙，请稍后重试`;
                    } else if (errorMsg.includes('超时') || errorMsg.includes('timeout')) {
                        icon = '⏱️';
                        message = `${event.agent} 执行超时`;
                    }
                }
                
                if (progressEl) {
                    const progressClass = event.status === 'success'
                        ? 'completed'
                        : (isAwaitingInput ? 'running' : 'error');
                    progressEl.innerHTML += `
                        <div class="progress-item ${progressClass}">
                            ${icon} ${message}
                        </div>
                    `;
                }
            }

            if (state.currentProcessCard?.kind === 'secretary-continuation') {
                appendSecretaryContinuationActivity(
                    event.message || `${event.agent || '协作成员'} 正在处理。`,
                    ['error', 'failed'].includes(event.status) ? 'error' : 'working'
                );
            } else if (cardId && state.currentProcessCard) {
                const card = state.currentProcessCard;
                const stage = card.stages[2];
                let currentDetails = [];
                if (stage.data && stage.data.agents) {
                    currentDetails = [...stage.data.agents];
                }

                const agentDetail = formatExecutionProgress(
                    event.agent,
                    event.status,
                    event.poll_count,
                    event.elapsed_time
                );

                const existingIndex = currentDetails.findIndex(d => d.includes(`<strong>${escapeHTML(event.agent)}</strong>`));
                if (existingIndex >= 0) {
                    currentDetails[existingIndex] = agentDetail;
                } else {
                    currentDetails.push(agentDetail);
                }

                const detailHTML = currentDetails.map(d => `<div style="margin-bottom: 0.25rem;">${d}</div>`).join('');
                updateProcessStage(cardId, 2, 'running', {
                    detail: detailHTML,
                    data: { agents: currentDetails }
                });
            }
            break;

        case 'execution_queued':
            setInlineExecutionStatus(event.message || '当前请求正在排队等待执行资源…', 'running');
            if (progressEl) {
                progressEl.innerHTML += `
                    <div class="progress-item running">
                        ⏳ ${escapeHTML(event.message || '当前请求正在排队等待执行资源…')}
                    </div>
                `;
            }
            if (cardId) {
                updateProcessStage(cardId, 2, 'running', {
                    detail: event.message || '当前请求正在排队等待执行资源…'
                });
            }
            break;

        case 'agent_result_card':
            const presentationMode = presentAgentResult(event);
            setInlineExecutionStatus(
                `${event.agent_name || event.agent || '智能体'}${isAgentAwaitingInput(event) ? '正在等待补充信息' : '已返回结果'}`,
                isAgentAwaitingInput(event) ? 'waiting' : 'completed'
            );
            if (isAgentAwaitingInput(event)) {
                setActiveAgentInteraction(event);
            } else if (isSameAgentInteraction(state.activeAgentInteraction, event)) {
                clearActiveAgentInteraction();
            }
            if (cardId) {
                const resultScope = getAgentDeploymentScope(event).selectionLabel;
                const resultAgentName = event.agent_name || event.agent || 'Agent';
                const resultState = String(event.state || event.status || '').toLowerCase();
                const resultFailed = ['failed', 'error', 'rejected', 'canceled', 'cancelled'].includes(resultState);
                const resultCompleted = ['completed', 'success', 'awaiting-completion', 'awaiting_completion'].includes(resultState);
                updateProcessCardIdentity(cardId, `${resultScope}：${resultAgentName}`);
                const detailText = presentationMode === 'agent-markdown-message'
                    ? `${escapeHTML(resultAgentName)} 已按 Markdown 呈现结果`
                    : `${escapeHTML(resultAgentName)} 正在执行`;
                updateProcessStage(cardId, 3, resultFailed ? 'error' : (resultCompleted ? 'completed' : 'running'), {
                    detail: detailText
                });
            }
            break;

        case 'subtask_result':
            applySubtaskResultEvent(event);
            break;

        case 'execution_complete':
            if (isNonCompletedExecutionComplete(event)) {
                console.warn('[Dingdang] Ignoring non-completed execution_complete event', event);
                updateAgentStatus(
                    event.agent_name || event.agent || '智能体',
                    event.protocol_state || event.protocolState || event.state,
                    event.poll_count,
                    event.elapsed_time
                );
                break;
            }
            finalizeRunningAgentStatuses();
            clearInlineExecutionStatus();
            state.pendingAgentCompletion = null;
            if (statusUpdateInterval) {
                clearInterval(statusUpdateInterval);
                statusUpdateInterval = null;
            }
            executionStartTime = null;
            agentPollCounts = {};
            agentElapsedTimes = {};
            finalizeAgentAppForTerminalEvent(event);
            if (progressEl) {
                progressEl.innerHTML += `<div class="progress-item completed">✅ 执行完成</div>`;
            }
            
            const statusContainer = document.getElementById('agentStatusContainer');
            if (statusContainer && state.activeExecutionAgentCount > 1 && event.total_elapsed_time !== undefined) {
                const totalTimeEl = document.createElement('div');
                totalTimeEl.className = 'agent-status-item status-total';
                totalTimeEl.innerHTML = `
                    <div class="status-header">
                        <span class="status-icon">⏱️</span>
                        <span class="status-agent-name">总执行时间</span>
                    </div>
                    <div class="status-details">
                        <span class="status-metric">${event.total_elapsed_time}s</span>
                    </div>
                `;
                statusContainer.appendChild(totalTimeEl);
            }
            
            if (event.content && !state.hasAgentResultCardsInCurrentRun && !state.hasRawAgentResultsInCurrentRun) {
                const meta = {};
                if (event.llm_time !== undefined) meta.llm_time = event.llm_time;
                if (event.total_elapsed_time !== undefined) meta.ds_time = event.total_elapsed_time;
                
                const html = `
                    <div class="message assistant">
                        <div class="message-avatar">
                            <img src="dingdang.png" alt="叮当">
                        </div>
                        <div class="message-content-wrapper">
                            <div class="message-content">${renderRichMessage(event.content)}</div>
                            ${renderCopyButton(event.content)}
                            ${renderMessageMeta(meta)}
                        </div>
                    </div>
                `;
                elements.messagesContainer.insertAdjacentHTML('beforeend', html);
            }
            
            const container = document.querySelector('.agent-selection-container');
            if (container) {
                const header = container.querySelector('.agent-selection-header');
                if (header && !container.querySelector('.btn-clear-panel')) {
                    const clearBtn = document.createElement('button');
                    clearBtn.className = 'btn-text btn-clear-panel';
                    clearBtn.textContent = '清除面板';
                    clearBtn.onclick = function() {
                        container.style.display = 'none';
                        state.selectedIoaAgents.clear();
                        state.currentIoaAgents = [];
                    };
                    header.appendChild(clearBtn);
                }
            }

            if (state.currentProcessCard?.kind === 'secretary-continuation') {
                setSecretaryContinuationState('completed', event.message || '叮当已完成本轮协作。');
            } else if (cardId) {
                let detail = '执行完成';
                if (event.total_elapsed_time !== undefined) {
                    detail += ` · 总用时 ${event.total_elapsed_time}s`;
                }
                updateProcessStage(cardId, 2, 'completed', {});
                updateProcessStage(cardId, 3, 'completed', {
                    detail: state.hasAgentResultCardsInCurrentRun
                        ? `${escapeHTML(event.agent_name || event.agent || state.currentProcessCard.identity || '云端智能体')} 已返回最终结果${event.task_id ? `<br><small>Task ${escapeHTML(event.task_id)}</small>` : ''}`
                        : state.hasRawAgentResultsInCurrentRun
                            ? 'ACPs 原始内容已呈现'
                            : '结果已呈现'
                });
            }
            break;

        case 'execution_waiting_input':
            if (statusUpdateInterval) {
                clearInterval(statusUpdateInterval);
                statusUpdateInterval = null;
            }
            executionStartTime = null;
            setInlineExecutionStatus(event.message || '智能体正在等待补充信息', 'waiting');
            updateAgentStatus(
                event.agent_name || event.agent || '智能体',
                'awaiting-input',
                event.poll_count,
                event.total_elapsed_time
            );
            if (progressEl) {
                progressEl.innerHTML += `<div class="progress-item running">⌁ ${escapeHTML(event.message || '智能体正在等待补充信息')}</div>`;
            }
            if (state.currentProcessCard?.kind === 'secretary-continuation') {
                setSecretaryContinuationState('waiting', event.message || '叮当正在等待补充信息。');
            } else if (cardId) {
                updateProcessStage(cardId, 2, 'running', {
                    detail: event.message || '等待用户补充信息'
                });
                updateProcessStage(cardId, 3, 'running', {
                    detail: `${escapeHTML(event.agent_name || event.agent || '智能体')} 正在等待补充信息`
                });
            }
            if (event.interaction_kind === 'collaboration' && event.collaboration) {
                const question = event.content || event.message || '请补充完成协作所需的信息。';
                state.pendingSecretaryCollaboration = {
                    userQuery: event.collaboration.user_query || state.currentTaskIntent.queryText || '',
                    selectedAgents: event.collaboration.selected_agents || [],
                    ioaAgents: event.collaboration.ioa_agents || [],
                    attachmentIds: event.collaboration.attachment_ids || [],
                    question,
                    runId: event.run_id || '',
                    candidateContent: event.candidate_content || ''
                };
                addMessageToUI({ role: 'assistant', content: `叮当需要你补充确认：\n\n${question}\n\n请直接在下方输入框回复，我会继续本次多智能体协作。` });
                state.messages.push({ role: 'assistant', content: question });
                setActiveAgentInteraction({ ...event, agent: '叮当', agent_name: '叮当', collaboration: state.pendingSecretaryCollaboration });
            } else if (event.quota_continuation_token) {
                const ref = getAgentResultCardDomId(event);
                const previous = state.agentResultEvents[ref];
                const interaction = { ...previous, ...event, quota_continuation_token: event.quota_continuation_token };
                rememberAgentResultEvent(interaction);
                setActiveAgentInteraction(interaction);
            } else if (event.task_id || event.taskId) {
                setActiveAgentInteraction(event);
            }
            break;

        case 'execution_awaiting_completion': {
            setInlineExecutionStatus(event.message || '候选方案正在等待确认', 'waiting');
            if (!isCollaborationCompletionEvent(event)) {
                renderExternalAgentCompletionPrompt(event);
                break;
            }
            const collaboration = event.collaboration || {};
            state.pendingSecretaryCompletion = { taskId: event.task_id || '', runId: event.run_id || '', collaborationTaskId: event.collaboration_task_id || '', nodeSource: event.node_source || 'dingdang', agent: event.agent_name || event.agent || '', endpoint: event.endpoint || '', aic: event.aic || '', sessionId: event.session_id || '', cardId: event.card_id || '', content: event.content || '', question: event.question || '', collaboration: { userQuery: collaboration.user_query || state.currentTaskIntent.queryText || '', selectedAgents: collaboration.selected_agents || [], ioaAgents: collaboration.ioa_agents || [], attachmentIds: collaboration.attachment_ids || [], question: event.question || '请补充偏好。', candidateContent: event.content || '' } };
            const prompt = document.createElement('div');
            prompt.className = 'message assistant secretary-completion-prompt';
            const candidateContent = event.content
                ? `<section class="secretary-completion-body" aria-label="候选方案"><div class="secretary-completion-section-title"><span aria-hidden="true">✦</span> 候选方案</div><div class="secretary-completion-candidate">${renderRichMessage(event.content)}</div></section>`
                : `<section class="secretary-completion-body secretary-completion-empty"><div class="secretary-completion-section-title"><span aria-hidden="true">✦</span> 候选方案</div><p>叮当正在整理可供确认的方案内容。</p></section>`;
            const followUp = event.question
                ? `<aside class="secretary-completion-question"><div class="secretary-completion-question-label">还需要你确认</div>${renderMarkdown(event.question)}</aside>`
                : '';
            const confirmAction = renderDynamicActionAttributes('confirm-secretary-completion', {});
            const continueAction = renderDynamicActionAttributes('continue-secretary-completion', {});
            prompt.innerHTML = `<div class="message-avatar"><img src="dingdang.png" alt="叮当"></div><div class="message-content-wrapper"><div class="message-content"><article class="secretary-completion-card"><header class="secretary-completion-header"><div><div class="secretary-completion-eyebrow"><span class="secretary-completion-pulse" aria-hidden="true"></span>叮当协作</div><h3>候选方案等待确认</h3></div><span class="secretary-completion-status is-flowing">待确认</span></header><div class="secretary-completion-summary">${escapeHTML(event.message || '请确认当前方案，或补充新的偏好。')}</div>${candidateContent}${followUp}<footer class="secretary-completion-footer"><span class="secretary-completion-hint">确认后将按当前方案继续。</span><div class="secretary-completion-actions"><button type="button" class="btn-primary" ${confirmAction}>确认按当前方案完成</button><button type="button" class="btn-secondary" ${continueAction}>继续补充偏好</button></div></footer></article></div></div>`;
            elements.messagesContainer.appendChild(prompt);
            scrollToBottom();
            break;
        }

        case 'execution_error':
            if (statusUpdateInterval) {
                clearInterval(statusUpdateInterval);
                statusUpdateInterval = null;
            }
            executionStartTime = null;
            clearInlineExecutionStatus();
            if (event.run_id) markCollaborationRunFailed(event.run_id, event.message);
            if (state.currentProcessCard?.kind === 'secretary-continuation') {
                setSecretaryContinuationState('error', event.message || '协作续办发生错误。');
            }
            if (event.agent_name || event.agent) {
                updateAgentStatus(event.agent_name || event.agent, 'error', event.poll_count, event.total_elapsed_time);
            }
            finalizeAgentAppForTerminalEvent(event);
            if (progressEl) {
                progressEl.innerHTML += `<div class="progress-item error">❌ ${event.message}</div>`;
            }

            if (cardId && state.currentProcessCard?.kind !== 'secretary-continuation') {
                updateProcessStage(cardId, 2, 'error', {
                    detail: event.message || '执行出错'
                });
                updateProcessStage(cardId, 3, 'error', {
                    detail: `${escapeHTML(event.agent_name || event.agent || '云端智能体')} 已返回失败终态${event.task_id ? `<br><small>Task ${escapeHTML(event.task_id)}</small>` : ''}`
                });
            }
            break;
    }

    scrollToBottom();
}

// 创建包含 HTTP 状态码和简短响应体的错误，避免 504/404 这类问题只显示“正在思考”。
async function createHttpError(response, actionText) {
    let bodyText = '';
    try {
        bodyText = await response.text();
    } catch (e) {
        bodyText = '';
    }
    
    if (response.status === 429) {
        return new Error('今日使用次数已达上限，请明天再试。如需增加限额，请联系管理员。');
    }
    
    let detail = null;
    try {
        const parsed = bodyText ? JSON.parse(bodyText) : null;
        detail = parsed?.detail || parsed?.error || null;
        if (detail?.error && typeof detail.error === 'object') detail = detail.error;
    } catch (e) {
        detail = null;
    }

    const friendlyMessage = typeof detail?.message === 'string'
        ? detail.message
        : `服务暂时无法完成请求（HTTP ${response.status}），请稍后重试。`;
    const rawDiagnostic = typeof detail?.raw_message === 'string'
        ? detail.raw_message
        : bodyText.slice(0, 500);
    const diagnosticText = rawDiagnostic && rawDiagnostic !== friendlyMessage
        ? `\n\n原始诊断：${rawDiagnostic}`
        : '';
    return new Error(`${friendlyMessage}${diagnosticText}`);
}

function normalizeUserFacingErrorText(text) {
    const rawText = String(text || '').trim();
    if (!rawText) return '操作未完成，请稍后重试。';
    if (rawText.includes('原始诊断：')) return rawText;

    const technicalError = /(HTTP\s*\d|\b(?:Error|Exception|Traceback)\b|failed to fetch|networkerror|validation error)/i.test(rawText);
    if (!technicalError) return rawText;

    return `操作未完成，请稍后重试；若持续失败请联系管理员。\n\n原始诊断：${rawText}`;
}

// 将当前 ACPs 调用过程卡片中正在执行或下一个阶段标记为失败。
function markCurrentProcessCardError(message) {
    message = normalizeUserFacingErrorText(message);
    const card = state.currentProcessCard;
    if (!card || !card.id) {
        return;
    }
    if (card.kind === 'secretary-continuation') {
        setSecretaryContinuationState('error', message || '协作续办请求失败');
        return;
    }
    let errorStage = 0;
    for (let i = card.stages.length - 1; i >= 0; i--) {
        if (card.stages[i].status === 'running') {
            errorStage = i;
            break;
        }
        if (card.stages[i].status === 'completed' && i < card.stages.length - 1) {
            errorStage = i + 1;
            break;
        }
    }
    updateProcessStage(card.id, errorStage, 'error', {
        detail: escapeHTML(message || '请求失败')
    });
}

// 更新执行进度
function updateExecutionProgress(message) {
    setInlineExecutionStatus(message, 'error');
}

function isFlowingStatus(stateName) {
    return new Set(['running', 'working', 'discovering', 'waiting', 'awaiting-input', 'awaiting-completion'])
        .has(String(stateName || '').toLowerCase());
}

function setInlineExecutionStatus(message, stateName = 'running') {
    if (stateName === 'error') message = normalizeUserFacingErrorText(message);
    const text = String(message || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    const existing = document.getElementById('conversationPhaseStatus');
    if (!text) {
        existing?.remove();
        return false;
    }
    const status = existing || document.createElement('div');
    if (!existing) {
        status.id = 'conversationPhaseStatus';
        status.className = 'conversation-phase-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        elements.messagesContainer.appendChild(status);
    }
    // 每次阶段更新都放到当前消息流末尾，旧阶段只保留这一处并被新文案替换。
    elements.messagesContainer.appendChild(status);
    status.dataset.state = stateName;
    status.classList.toggle('is-flowing', isFlowingStatus(stateName));
    status.textContent = text;
    scrollToBottom();
    return true;
}

function clearInlineExecutionStatus() {
    return setInlineExecutionStatus('');
}

// 添加错误消息
function addErrorMessage(text) {
    const displayText = normalizeUserFacingErrorText(text);
    const html = `
        <div class="message assistant">
            <div class="message-avatar">
                <img src="dingdang.png" alt="叮当">
            </div>
            <div class="message-content-wrapper">
                <div class="message-content" style="color: #ef4444;">${escapeHTML(displayText)}</div>
                ${renderCopyButton(displayText)}
            </div>
        </div>
    `;
    elements.messagesContainer.insertAdjacentHTML('beforeend', html);
    scrollToBottom();
}

// 滚动到底部
function scrollToBottom() {
    elements.messagesContainer.scrollTop = elements.messagesContainer.scrollHeight;
}

const PROCESS_STAGES = [
    { index: 0, icon: '🧠', title: '意图识别', defaultStatus: '待执行' },
    { index: 1, icon: '🔍', title: '能力发现', defaultStatus: '待执行' },
    { index: 2, icon: '⚡', title: 'AIP调用', defaultStatus: '待执行' },
    { index: 3, icon: '✅', title: '结果返回', defaultStatus: '待执行' }
];

// 协作续办不是一次新的 Discovery/AIP 调用：它代表叮当在既有协作
// 上吸收用户补充、重新编排并恢复委派。因此使用独立的展示语义。
function createSecretaryContinuationCard(conversationId, pending, userAnswer) {
    const cardId = 'secretary-continuation-' + Date.now();
    const participants = Array.isArray(pending?.selectedAgents) ? pending.selectedAgents : [];
    state.currentProcessCard = {
        id: cardId,
        kind: 'secretary-continuation',
        conversationId: conversationId || null,
        activities: ['已接收你的补充，叮当正在恢复多智能体协作上下文。']
    };
    setInlineExecutionStatus('已接收补充，叮当正在恢复多智能体协作上下文。', 'running');
    return cardId;
}

function appendSecretaryContinuationActivity(message, stateName = 'working') {
    const card = state.currentProcessCard;
    if (!card || card.kind !== 'secretary-continuation' || !message) return;
    if (card.activities[card.activities.length - 1] === message) return;
    card.activities.push(message);
    setInlineExecutionStatus(message, stateName === 'error' ? 'error' : 'running');
}

function setSecretaryContinuationState(stateName, message = '') {
    const card = state.currentProcessCard;
    if (!card || card.kind !== 'secretary-continuation') return;
    if (message) appendSecretaryContinuationActivity(message, stateName);
    else setInlineExecutionStatus(
        stateName === 'completed' ? '协作已完成' : stateName === 'waiting' ? '等待补充信息' : stateName === 'error' ? '协作续办失败' : '正在恢复协作',
        stateName === 'error' ? 'error' : (stateName === 'completed' ? 'completed' : 'running')
    );
}

function createProcessCard(conversationId, userInput, options = {}) {
    const cardId = 'process-card-' + Date.now();
    state.currentProcessCard = {
        id: cardId,
        conversationId: conversationId || null,
        userInput: userInput || '',
        title: options.title || 'ACPs 调用过程',
        identity: options.identity || '',
        stages: (options.stages || PROCESS_STAGES).map(s => ({
            ...s,
            status: 'pending',
            detail: '',
            data: {}
        })),
        collapsed: false
    };

    state.currentProcessCard.stages[0].status = 'running';
    state.currentProcessCard.stages[0].detail = '正在分析用户意图...';

    setInlineExecutionStatus('正在分析用户意图…', 'running');
    return cardId;
}

function createProcessCardHTML(cardData) {
    const stagesHTML = cardData.stages.map((stage, index) => {
        const statusClass = stage.status || 'pending';
        let statusText = stage.defaultStatus;
        if (statusClass === 'running') statusText = '进行中';
        else if (statusClass === 'completed') statusText = '已完成';
        else if (statusClass === 'error') statusText = '失败';

        let detailHTML = '';
        if (stage.detail) {
            if (Array.isArray(stage.detail)) {
                detailHTML = `
                    <div class="process-stage-detail">
                        <ul>
                            ${stage.detail.map(item => `<li>${item}</li>`).join('')}
                        </ul>
                    </div>
                `;
            } else {
                detailHTML = `
                    <div class="process-stage-detail">
                        ${stage.detail}
                    </div>
                `;
            }
        }

        return `
            <div class="process-stage ${statusClass}" data-stage-index="${index}">
                <div class="process-stage-dot"></div>
                <div class="process-stage-content">
                    <div class="process-stage-title">
                        <span>${stage.icon}</span>
                        <span>${stage.title}</span>
                    </div>
                    <div class="process-stage-status">${statusText}</div>
                    ${detailHTML}
                </div>
            </div>
        `;
    }).join('');

    return `
        <div id="${escapeHTMLAttribute(cardData.id)}" class="process-card" data-conversation-id="${escapeHTMLAttribute(cardData.conversationId || '')}">
            <div class="process-card-header">
                <div class="process-card-title">
                    <span class="process-card-title-icon">⚙️</span>
                    <span>${escapeHTML(cardData.title || 'ACPs 调用过程')}</span>
                    ${cardData.identity ? `<small class="process-card-identity">${escapeHTML(cardData.identity)}</small>` : ''}
                </div>
                <button class="process-card-toggle ${cardData.collapsed ? 'collapsed' : ''}" aria-label="展开/收起">
                    ▼
                </button>
            </div>
            <div class="process-card-body ${cardData.collapsed ? 'collapsed' : ''}">
                <div class="process-timeline">
                    ${stagesHTML}
                </div>
            </div>
        </div>
    `;
}

// Discovery 返回真实 Agent 后更新过程卡身份，加载态和结果态不会继续保留泛称。
function updateProcessCardIdentity(cardId, identity) {
    const card = state.currentProcessCard;
    if (!card || card.id !== cardId || !identity) return;
    card.identity = identity;
}

function updateProcessStage(cardId, stageIndex, status, data) {
    const card = state.currentProcessCard;
    if (!card || card.id !== cardId) return;

    const stage = card.stages[stageIndex];
    if (!stage) return;

    stage.status = status;

    if (data) {
        if (data.detail !== undefined) {
            stage.detail = data.detail;
        }
        if (data.data !== undefined) {
            stage.data = data.data;
        }
    }

    const stateName = status === 'error' ? 'error' : (status === 'completed' ? 'completed' : 'running');
    const detail = String(stage.detail || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    setInlineExecutionStatus(
        status === 'error'
            ? `${stage.title}失败${detail ? `：${detail}` : ''}`
            : status === 'completed'
                ? `${stage.title}已完成`
                : `${stage.title}：${detail || '正在处理…'}`,
        stateName
    );

    const cardEl = document.getElementById(cardId);
    if (!cardEl) return;

    const stageEl = cardEl.querySelector(`.process-stage[data-stage-index="${stageIndex}"]`);
    if (!stageEl) return;

    stageEl.classList.remove('pending', 'running', 'completed', 'error');
    stageEl.classList.add(status);

    let statusText = '待执行';
    if (status === 'running') statusText = '进行中';
    else if (status === 'completed') statusText = '已完成';
    else if (status === 'error') statusText = '失败';

    const statusEl = stageEl.querySelector('.process-stage-status');
    if (statusEl) {
        statusEl.textContent = statusText;
    }

    let detailEl = stageEl.querySelector('.process-stage-detail');
    if (stage.detail) {
        if (!detailEl) {
            detailEl = document.createElement('div');
            detailEl.className = 'process-stage-detail';
            const contentEl = stageEl.querySelector('.process-stage-content');
            if (contentEl) {
                contentEl.appendChild(detailEl);
            }
        }

        if (Array.isArray(stage.detail)) {
            detailEl.innerHTML = `
                <ul>
                    ${stage.detail.map(item => `<li>${item}</li>`).join('')}
                </ul>
            `;
        } else {
            detailEl.innerHTML = stage.detail;
        }
    } else if (detailEl) {
        detailEl.remove();
    }
}

function toggleProcessCard(cardId) {
    const card = state.currentProcessCard;
    if (!card || card.id !== cardId) return;

    card.collapsed = !card.collapsed;

    const cardEl = document.getElementById(cardId);
    if (!cardEl) return;

    const bodyEl = cardEl.querySelector('.process-card-body');
    const toggleEl = cardEl.querySelector('.process-card-toggle');

    if (bodyEl) {
        bodyEl.classList.toggle('collapsed', card.collapsed);
    }
    if (toggleEl) {
        toggleEl.classList.toggle('collapsed', card.collapsed);
    }
}

function formatAgentTags(webAgents, ioaAgents) {
    const tags = [];
    if (webAgents && webAgents.length > 0) {
        webAgents.forEach(agent => {
            tags.push(`<span class="agent-tag">🌐 ${escapeHTML(agent.name || agent)}</span>`);
        });
    }
    if (ioaAgents && ioaAgents.length > 0) {
        ioaAgents.forEach(agent => {
            tags.push(`<span class="agent-tag">🔧 ${escapeHTML(agent.name || agent)}</span>`);
        });
    }
    return tags.join('');
}

function formatExecutionProgress(agent, status, pollCount, elapsedTime) {
    const statusMap = {
        'running': { text: '调用中', class: 'running' },
        'success': { text: '成功', class: 'success' },
        'error': { text: '失败', class: 'error' },
        'awaiting-input': { text: '待输入', class: 'running' },
        'accepted': { text: '已接受', class: 'running' },
        'working': { text: '工作中', class: 'running' },
        'rejected': { text: '已拒绝', class: 'error' }
    };
    const statusInfo = statusMap[status] || statusMap['running'];

    let detail = `<strong>${escapeHTML(agent)}</strong> <span class="status-badge ${statusInfo.class}">${statusInfo.text}</span>`;
    if (pollCount !== undefined && pollCount !== null) {
        detail += ` · 轮询 ${pollCount} 次`;
    }
    if (elapsedTime !== undefined && elapsedTime !== null) {
        detail += ` · 用时 ${elapsedTime}s`;
    }
    return detail;
}

// “随意探索”是一次有意图的用户操作；草稿态需要先取得真实会话，
// 已打开的会话则保持原样，避免探索结果跑到无归属的临时面板中。
async function ensureExploreConversation() {
    if (state.currentConversationId) return state.currentConversationId;
    const response = await fetch(`${API_BASE}/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify({ title: '随意探索' })
    });
    if (!response.ok) throw await createHttpError(response, '创建探索对话');
    const conversation = await response.json();
    if (!conversation?.id) throw new Error('创建探索对话后未返回会话标识');
    adoptConversationId(conversation.id);
    await loadConversationHistory();
    return conversation.id;
}

// 随意探索功能
async function exploreAgents() {
    if (!state.user) {
        showAuthModal();
        return;
    }

    if (state.isLoading) return;
    
    state.isLoading = true;
    const btn = elements.exploreBtn;
    if (btn) {
        btn.disabled = true;
        btn.textContent = '探索中...';
    }

    try {
        const conversationId = await ensureExploreConversation();
        const response = await fetch(`${API_BASE}/discover/trending`, {
            method: 'GET',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('获取推荐智能体失败');
        }

        const agents = await response.json();
        
        if (elements.messagesContainer.querySelector('.welcome-message')) {
            clearDynamicActionElement(elements.messagesContainer);
        }

        if (agents.length === 0) {
            addErrorMessage('暂无推荐的智能体');
            return;
        }

        const webAgents = agents.filter(a => a.type === 'web');
        const ioaAgents = agents.filter(a => a.type === 'ioa');
        
        const html = `
            <div class="message assistant">
                <div class="message-avatar">
                    <img src="dingdang.png" alt="叮当">
                </div>
                <div class="message-content-wrapper">
                    <div class="message-content">为你推荐以下热门智能体：</div>
                </div>
            </div>
        `;
        elements.messagesContainer.insertAdjacentHTML('beforeend', html);
        state.messages.push({ role: 'assistant', content: '为你推荐以下热门智能体：' });
        
        addAgentSelectionPanel(webAgents, ioaAgents, '', conversationId);
        
    } catch (e) {
        console.error('Error exploring agents:', e);
        addErrorMessage('探索失败，请稍后重试');
    } finally {
        state.isLoading = false;
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🎲 随意探索';
        }
    }
}

// Tab 切换功能
function switchTab(tabName) {
    if (tabName === 'profile') {
        if (state.user) showUserInfoModal();
        else showAuthModal();
        return;
    }
    if (tabName === 'discover') {
        const kind = state.currentIoaAgents.length ? 'ioa' : 'web';
        if (getAgentCatalogAgents(kind).length) showAgentCatalogModal(kind);
        else exploreAgents();
        return;
    }
    scrollToBottom();
}

// 校验图片文件
function validateImageFile(file) {
    const validTypes = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp'];
    if (!validTypes.includes(file.type)) {
        alert('不支持的图片格式，请上传 JPG、PNG、GIF 或 WebP 格式的图片');
        return false;
    }
    const maxSize = 10 * 1024 * 1024;
    if (file.size > maxSize) {
        alert('图片大小不能超过 10MB');
        return false;
    }
    return true;
}

// 上传图片到服务器
async function uploadImage(file) {
    const formData = new FormData();
    formData.append('file', file);

    const response = await fetch(`${API_BASE}/upload/image`, {
        method: 'POST',
        headers: {
            ...getAuthHeaders()
        },
        body: formData
    });

    if (!response.ok) {
        throw await createHttpError(response, '上传图片');
    }

    return await response.json();
}

function validateAttachmentFile(file) {
    const validTypes = [
        'image/jpeg', 'image/png', 'image/gif', 'image/webp',
        'application/pdf', 'text/plain', 'text/markdown', 'application/json',
        'audio/wav', 'audio/mpeg', 'audio/ogg'
    ];
    if (!validTypes.includes(file.type)) {
        alert('不支持的附件格式');
        return false;
    }
    if (file.size > 25 * 1024 * 1024) {
        alert('附件大小不能超过 25MB');
        return false;
    }
    return true;
}

async function uploadAttachment(file) {
    const formData = new FormData();
    formData.append('file', file);
    const response = await fetch(`${API_BASE}/upload/attachments`, {
        method: 'POST',
        headers: { ...getAuthHeaders() },
        body: formData
    });
    if (!response.ok) throw await createHttpError(response, '上传附件');
    return await response.json();
}

async function handleAttachmentFile(file) {
    if (file.type.startsWith('image/') && !state.currentImage.artifactId && !state.currentImage.uploading) {
        await handleImageFile(file);
        return;
    }
    if (!validateAttachmentFile(file)) return;
    const attachment = {
        artifactId: null,
        filename: file.name,
        mimeType: file.type,
        status: '上传中',
        uploading: true
    };
    state.currentAttachments.push(attachment);
    showAttachmentPreview();
    try {
        const uploaded = await uploadAttachment(file);
        attachment.artifactId = uploaded.artifact_id;
        attachment.status = uploaded.status === 'processing' ? '处理中' : '已就绪';
        attachment.uploading = false;
        showAttachmentPreview();
    } catch (error) {
        state.currentAttachments = state.currentAttachments.filter(item => item !== attachment);
        showAttachmentPreview();
        alert('附件上传失败：' + (error.message || '请稍后重试'));
    }
}

function showAttachmentPreview() {
    if (!elements.attachmentPreview) return;
    if (state.currentAttachments.length === 0) {
        elements.attachmentPreview.style.display = 'none';
        return;
    }
    elements.attachmentPreview.style.display = 'flex';
    elements.attachmentPreview.innerHTML = state.currentAttachments.map((item, index) => `
        <span class="attachment-preview-item">
            <span class="attachment-preview-icon">📎</span>
            <span>${escapeHTML(item.filename || '附件')}</span>
            <span>${escapeHTML(item.status || '已就绪')}</span>
            <button type="button" data-attachment-remove="${index}" aria-label="删除附件">×</button>
        </span>
    `).join('');
}

function clearCurrentAttachment(index = null) {
    if (Number.isInteger(index)) {
        state.currentAttachments.splice(index, 1);
    } else {
        state.currentAttachments = [];
    }
    showAttachmentPreview();
}

// 处理选中的图片文件
async function handleImageFile(file) {
    if (!validateImageFile(file)) {
        return;
    }

    state.currentImage.file = file;
    state.currentImage.previewUrl = URL.createObjectURL(file);
    state.currentImage.uploading = true;
    state.currentImage.imageUrl = null;
    state.currentImage.artifactId = null;

    showImagePreview();

    try {
        const uploaded = await uploadImage(file);
        state.currentImage.imageUrl = uploaded.url || uploaded.image_url || uploaded.imageUrl;
        state.currentImage.artifactId = uploaded.artifact_id || uploaded.id || null;
        state.currentImage.uploading = false;
        hideImageUploading();
    } catch (e) {
        console.error('Image upload error:', e);
        alert('图片上传失败：' + (e.message || '请稍后重试'));
        clearCurrentImage();
    }
}

// 显示图片预览
function showImagePreview() {
    if (elements.imagePreview && state.currentImage.previewUrl) {
        elements.imagePreview.src = state.currentImage.previewUrl;
    }
    if (elements.imagePreviewWrapper) {
        elements.imagePreviewWrapper.style.display = 'block';
    }
    if (state.currentImage.uploading) {
        showImageUploading();
    }
}

// 隐藏图片预览
function hideImagePreview() {
    if (elements.imagePreviewWrapper) {
        elements.imagePreviewWrapper.style.display = 'none';
    }
}

// 显示上传中状态
function showImageUploading() {
    if (elements.imageUploadingOverlay) {
        elements.imageUploadingOverlay.style.display = 'flex';
    }
}

// 隐藏上传中状态
function hideImageUploading() {
    if (elements.imageUploadingOverlay) {
        elements.imageUploadingOverlay.style.display = 'none';
    }
}

// 清除当前选中的图片
function clearCurrentImage() {
    if (state.currentImage.previewUrl) {
        URL.revokeObjectURL(state.currentImage.previewUrl);
    }
    state.currentImage = { file: null, previewUrl: null, imageUrl: null, artifactId: null, uploading: false };
    if (elements.imageInput) {
        elements.imageInput.value = '';
    }
    hideImagePreview();
}

// 触发图片选择器
function triggerImageUpload() {
    if (elements.imageInput) {
        elements.imageInput.click();
    }
}

// 处理拖拽上传
function handleDragOver(e) {
    e.preventDefault();
    e.stopPropagation();
    const chatView = document.querySelector('.tab-view-chat');
    if (chatView) {
        chatView.classList.add('drag-over');
    }
}

function handleDragLeave(e) {
    e.preventDefault();
    e.stopPropagation();
    const chatView = document.querySelector('.tab-view-chat');
    if (chatView) {
        chatView.classList.remove('drag-over');
    }
}

function handleDrop(e) {
    e.preventDefault();
    e.stopPropagation();
    const chatView = document.querySelector('.tab-view-chat');
    if (chatView) {
        chatView.classList.remove('drag-over');
    }

    const files = Array.from(e.dataTransfer?.files || []);
    files.forEach(file => handleAttachmentFile(file));
}

// 设置事件监听器
function setupEventListeners() {
    elements.loginBtn?.addEventListener('click', showAuthModal);
    elements.closeModal.addEventListener('click', hideAuthModal);
    elements.authModal.addEventListener('click', (e) => {
        if (e.target === elements.authModal) hideAuthModal();
    });
    elements.switchMode.addEventListener('click', toggleAuthMode);
    elements.authForm.addEventListener('submit', handleAuthSubmit);
    if (elements.agentRenderToggle) {
        elements.agentRenderToggle.addEventListener('click', toggleRealtimeAgentRendering);
    }
    elements.sendBtn.addEventListener('click', endCurrentConversationTask);
    if (elements.agentInteractionEndBtn) {
        elements.agentInteractionEndBtn.addEventListener('click', endActiveAgentInteraction);
    }
    elements.imageUploadBtn.addEventListener('click', triggerImageUpload);
    elements.imageInput.addEventListener('change', (e) => {
        const files = Array.from(e.target.files || []);
        files.forEach(file => handleAttachmentFile(file));
        e.target.value = '';
    });
    elements.imageRemoveBtn.addEventListener('click', clearCurrentImage);
    if (elements.attachmentPreview) {
        elements.attachmentPreview.addEventListener('click', event => {
            const button = event.target.closest('[data-attachment-remove]');
            if (button) clearCurrentAttachment(Number(button.dataset.attachmentRemove));
        });
    }
    elements.messageInput.addEventListener('keydown', (e) => {
        // During a Chinese/Japanese/Korean IME composition, Enter confirms a
        // candidate. It must never be interpreted as a chat submit shortcut.
        if (e.isComposing || e.keyCode === 229 || e.key === 'Process') return;
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    elements.messageInput.addEventListener('compositionend', () => {
        // Kept as an explicit composition listener for IMEs that dispatch the
        // final input asynchronously after candidate confirmation.
        elements.messageInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    window.addEventListener('focus', () => { ensureFreshAccessToken().catch(() => {}); });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) ensureFreshAccessToken().catch(() => {});
    });
    elements.messageInput.addEventListener('input', () => {
        if (UI_HELPERS.resizeTextareaToContent) {
            UI_HELPERS.resizeTextareaToContent(elements.messageInput, MESSAGE_INPUT_MAX_HEIGHT);
        }
    });
    elements.messagesContainer.addEventListener('click', async (event) => {
        const imageTrigger = event.target.closest('[data-message-image-open]');
        if (imageTrigger) {
            const wrapper = imageTrigger.closest('[data-message-resource-id]');
            openMessageImageLightbox(wrapper?.dataset.messageResourceId, imageTrigger);
            return;
        }

        const downloadButton = event.target.closest('[data-message-download]');
        if (!downloadButton || downloadButton.disabled) return;
        const resourceId = downloadButton.dataset.messageResourceId;
        downloadButton.disabled = true;
        try {
            await downloadMessageResource(resourceId);
        } finally {
            downloadButton.disabled = false;
        }
    });
    elements.messagesContainer.addEventListener('error', handleMessageResourceImageError, true);
    elements.messageImageLightboxClose?.addEventListener('click', closeMessageImageLightbox);
    elements.messageImageLightbox?.addEventListener('click', (event) => {
        if (event.target === elements.messageImageLightbox) closeMessageImageLightbox();
    });
    elements.closeUserInfoModal.addEventListener('click', hideUserInfoModal);
    elements.userInfoModal.addEventListener('click', (e) => {
        if (e.target === elements.userInfoModal) hideUserInfoModal();
    });
    elements.copyUserInfoAic?.addEventListener('click', async () => {
        const aic = state.user?.aip_entity_aic;
        if (!aic) return;
        try {
            await navigator.clipboard.writeText(aic);
            elements.userInfoAicFeedback.textContent = '已复制 AIC';
        } catch (error) {
            elements.userInfoAicFeedback.textContent = '复制失败，请选中 AIC 手动复制';
        }
    });
    // 修改密码相关事件
    elements.changePasswordEntryBtn.addEventListener('click', async () => {
        if (state.authProvider === 'keycloak') {
            try {
                const response = await fetch(`${API_BASE}/auth/account-url`, { headers: getAuthHeaders() });
                if (!response.ok) throw new Error('无法打开账号中心');
                const { url } = await response.json();
                window.location.assign(url);
            } catch (error) {
                alert(error.message || '无法打开 Keycloak 账号中心');
            }
            return;
        }
        showChangePasswordView();
    });
    if (elements.profileAdminUsersBtn) {
        elements.profileAdminUsersBtn.addEventListener('click', () => {
            hideUserInfoModal();
            showAdminUsersModal();
        });
    }
    if (elements.profileAdminLogsBtn) {
        elements.profileAdminLogsBtn.addEventListener('click', () => {
            hideUserInfoModal();
            showAdminLogsModal();
        });
    }
    elements.backToUserInfoBtn.addEventListener('click', hideChangePasswordView);
    elements.changePasswordForm.addEventListener('submit', handleChangePassword);
    elements.newPassword.addEventListener('input', () => {
        updatePasswordStrength();
        validateConfirmPassword();
        updateSubmitButtonState();
    });
    elements.confirmPassword.addEventListener('input', () => {
        validateConfirmPassword();
        updateSubmitButtonState();
    });
    elements.currentPassword.addEventListener('input', updateSubmitButtonState);
    // 密码显隐切换（三个输入框）
    document.querySelectorAll('.password-toggle-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            togglePasswordVisibility(btn.dataset.target);
        });
    });
    elements.closeAdminUsersModal.addEventListener('click', hideAdminUsersModal);
    elements.adminUsersModal.addEventListener('click', (e) => {
        if (e.target === elements.adminUsersModal) hideAdminUsersModal();
    });
    elements.adminUserSearchBtn.addEventListener('click', handleAdminUserSearch);
    elements.adminUserSearchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            handleAdminUserSearch();
        }
    });

    document.getElementById('closeEditQuotaModal').addEventListener('click', hideEditQuotaModal);
    document.getElementById('editQuotaModal').addEventListener('click', (e) => {
        if (e.target === document.getElementById('editQuotaModal')) hideEditQuotaModal();
    });
    document.getElementById('editQuotaSubmit').addEventListener('click', saveEditQuota);
    // 全选复选框
    document.getElementById('adminSelectAllCheckbox').addEventListener('change', (e) => {
        const rowCheckboxes = elements.adminUsersTableBody.querySelectorAll('.admin-row-checkbox:not(:disabled)');
        rowCheckboxes.forEach(cb => { cb.checked = e.target.checked; });
        updateSelectAllState();
    });

    // 批量操作按钮
    document.getElementById('adminBatchEnsureAicEntityBtn').addEventListener('click', batchEnsureAdminUserAicEntities);
    document.getElementById('adminBatchResetBtn').addEventListener('click', batchResetQuota);
    document.getElementById('adminBatchQuotaBtn').addEventListener('click', batchUpdateQuota);
    document.getElementById('adminBatchCancelBtn').addEventListener('click', clearSelection);

    // 批量调整限额弹窗
    document.getElementById('closeBatchQuotaModal').addEventListener('click', hideBatchQuotaModal);
    document.getElementById('batchQuotaModal').addEventListener('click', (e) => {
        if (e.target === document.getElementById('batchQuotaModal')) hideBatchQuotaModal();
    });
    document.getElementById('batchQuotaSubmit').addEventListener('click', saveBatchQuota);
    elements.adminPrevPageBtn.addEventListener('click', () => changeAdminUsersPage(-1));
    elements.adminNextPageBtn.addEventListener('click', () => changeAdminUsersPage(1));

    // 日志查看弹窗
    if (elements.closeAdminLogsModal) {
        elements.closeAdminLogsModal.addEventListener('click', hideAdminLogsModal);
    }
    if (elements.adminLogsModal) {
        elements.adminLogsModal.addEventListener('click', (e) => {
            if (e.target === elements.adminLogsModal) hideAdminLogsModal();
        });
    }
    if (elements.adminLogsSearchBtn) {
        elements.adminLogsSearchBtn.addEventListener('click', handleAdminLogsSearch);
    }
    if (elements.adminLogsKeyword) {
        elements.adminLogsKeyword.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                handleAdminLogsSearch();
            }
        });
    }
    if (elements.adminLogsPrevPageBtn) {
        elements.adminLogsPrevPageBtn.addEventListener('click', () => changeAdminLogsPage(-1));
    }
    if (elements.adminLogsNextPageBtn) {
        elements.adminLogsNextPageBtn.addEventListener('click', () => changeAdminLogsPage(1));
    }

    elements.exploreBtn.addEventListener('click', exploreAgents);
    if (elements.clearChatBtn) {
        elements.clearChatBtn.addEventListener('click', startNewConversation);
    }
    elements.recentConversations?.addEventListener('click', event => {
        const cancelDelete = event.target.closest('[data-history-delete-cancel]');
        if (cancelDelete) {
            event.preventDefault();
            event.stopPropagation();
            state.historyDeleteConfirmationId = null;
            renderConversationHistory();
            return;
        }
        const deleteRequest = event.target.closest('[data-history-delete-request]');
        if (deleteRequest) {
            event.preventDefault();
            event.stopPropagation();
            state.historyDeleteConfirmationId = deleteRequest.dataset.historyDeleteRequest;
            renderConversationHistory();
            return;
        }
        const confirmDelete = event.target.closest('[data-history-delete-confirm]');
        if (confirmDelete) {
            event.preventDefault();
            event.stopPropagation();
            void removeConversationHistory(confirmDelete.dataset.historyDeleteConfirm);
            return;
        }
        const button = event.target.closest('[data-conversation-id]');
        if (button) openConversation(button.dataset.conversationId);
    });
    if (elements.recentAgentAppBtn) {
        elements.recentAgentAppBtn.addEventListener('click', reopenRecentAgentApp);
    }
    if (elements.recentAgentAppDismissBtn) {
        elements.recentAgentAppDismissBtn.addEventListener('click', dismissRecentAgentAppEntry);
    }
    if (elements.weatherAgentBackBtn) {
        elements.weatherAgentBackBtn.addEventListener('click', closeWeatherAgentApp);
    }
    if (elements.agentAppPageList) {
        elements.agentAppPageList.addEventListener('click', (event) => {
            const pageButton = event.target.closest('.agent-app-page-item');
            if (!pageButton || !pageButton.dataset.pageKey) return;
            activateAgentAppPage(pageButton.dataset.pageKey);
        });
    }
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && elements.themeMenu && !elements.themeMenu.hidden) {
            e.preventDefault();
            closeThemeMenu({ restoreFocus: true });
        } else if (e.key === 'Escape' && elements.sidebarAccountMenu && !elements.sidebarAccountMenu.hidden) {
            e.preventDefault();
            closeSidebarAccountMenu({ restoreFocus: true });
        } else if (e.key === 'Escape' && elements.chatShell?.classList.contains('sidebar-open')) {
            e.preventDefault();
            setMobileSidebarOpen(false);
        } else if (e.key === 'Escape' && elements.messageImageLightbox && !elements.messageImageLightbox.hidden) {
            e.preventDefault();
            closeMessageImageLightbox();
        } else if (e.key === 'Escape' && state.weatherAgentApp.active) {
            closeWeatherAgentApp();
        }
    });

    // 头像入口统一打开账号对话框，不再切换到独立 Profile 页面。
    if (elements.dingdangAvatar) {
        elements.dingdangAvatar.addEventListener('click', () => {
            if (state.user) showUserInfoModal();
            else showAuthModal();
        });
    }

    // 发现页 - 搜索
    if (elements.discoverSearchBtn) {
        elements.discoverSearchBtn.addEventListener('click', handleDiscoverSearch);
    }
    if (elements.discoverSearchInput) {
        elements.discoverSearchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                handleDiscoverSearch();
            }
        });
    }

    // 发现页 - 分类标签
    if (elements.categoryTags) {
        elements.categoryTags.forEach(tag => {
            tag.addEventListener('click', () => {
                handleCategoryClick(tag.dataset.category);
            });
        });
    }

    // 发现页 - 发现更多按钮
    if (elements.discoverMoreBtn) {
        elements.discoverMoreBtn.addEventListener('click', handleDiscoverMore);
    }

    // 发现页 - 智能体卡片点击
    if (elements.discoverAgentsGrid) {
        elements.discoverAgentsGrid.addEventListener('click', (e) => {
            const card = e.target.closest('.discover-agent-card');
            if (card) {
                handleAgentCardClick(card);
            }
        });
    }

    // 我的页 - 菜单项点击
    if (elements.menuSettings) {
        elements.menuSettings.addEventListener('click', handleMenuSettings);
    }
    if (elements.menuAbout) {
        elements.menuAbout.addEventListener('click', handleMenuAbout);
    }

    // 我的页 - 退出登录
    if (elements.profileLogoutBtn) {
        elements.profileLogoutBtn.addEventListener('click', handleProfileLogout);
    }

    // 我的页 - 用户区域点击（未登录时跳转登录）
    const profileUserSection = document.querySelector('.profile-user-section');
    if (profileUserSection) {
        profileUserSection.addEventListener('click', () => {
            if (!state.user) {
                showAuthModal();
            } else {
                showUserInfoModal();
            }
        });
    }

    // 拖拽上传
    const chatView = document.querySelector('.tab-view-chat');
    if (chatView) {
        chatView.addEventListener('dragover', handleDragOver);
        chatView.addEventListener('dragleave', handleDragLeave);
        chatView.addEventListener('drop', handleDrop);
    }
}

async function refreshMissingUserInfoAic(userId) {
    try {
        if (!await ensureFreshAccessToken()) throw new Error('登录状态已过期');
        const response = await fetch(`${API_BASE}/auth/me`, {
            headers: getAuthHeaders(),
            cache: 'no-store'
        });
        if (!response.ok) throw new Error('无法读取账号资料');
        const user = await response.json();
        if (user.id !== userId) return;
        state.user = user;
        if (!elements.userInfoModal.classList.contains('active')) return;
        const aic = user.aip_entity_aic || '';
        elements.userInfoAic.textContent = aic || '尚未生成，请重新登录后重试';
        elements.copyUserInfoAic.disabled = !aic;
    } catch (error) {
        if (elements.userInfoModal.classList.contains('active') &&
            (!state.user || state.user.id === userId)) {
            elements.userInfoAic.textContent = '暂时无法读取，请重新登录后重试';
            elements.copyUserInfoAic.disabled = true;
        }
    }
}

// 显示用户信息弹窗
function showUserInfoModal() {
    if (!state.user) return;
    
    elements.userInfoAvatar.textContent = state.user.username.charAt(0).toUpperCase();
    elements.userInfoName.textContent = state.user.username;
    elements.userInfoStatus.textContent = state.user.role === 'admin' ? '管理员 · 已登录' : '已登录';
    const leaderAic = state.user.aip_entity_aic || '';
    elements.userInfoAic.textContent = leaderAic || '正在核实 AIC…';
    elements.copyUserInfoAic.disabled = !leaderAic;
    elements.userInfoAicFeedback.textContent = '';
    if (elements.userInfoAdminActions) {
        elements.userInfoAdminActions.classList.toggle('is-hidden', state.user.role !== 'admin');
    }
    
    elements.userInfoModal.classList.add('active');
    if (!leaderAic) void refreshMissingUserInfoAic(state.user.id);
}

// 隐藏用户信息弹窗
function hideUserInfoModal() {
    elements.userInfoModal.classList.remove('active');
    resetChangePasswordView();
}

// 切换到修改密码视图
function showChangePasswordView() {
    elements.userInfoView.classList.add('is-hidden');
    elements.changePasswordView.classList.remove('is-hidden');
    elements.changePasswordView.classList.add('is-visible');
    elements.changePasswordView.style.display = 'block';
    elements.userInfoView.style.display = 'none';
    resetChangePasswordForm();
}

// 切换回用户信息视图
function hideChangePasswordView() {
    elements.userInfoView.classList.remove('is-hidden');
    elements.changePasswordView.classList.add('is-hidden');
    elements.changePasswordView.classList.remove('is-visible');
    elements.userInfoView.style.display = 'block';
    elements.changePasswordView.style.display = 'none';
    resetChangePasswordForm();
}

// 清空修改密码表单与状态
function resetChangePasswordForm() {
    elements.currentPassword.value = '';
    elements.newPassword.value = '';
    elements.confirmPassword.value = '';
    elements.passwordStrengthBar.style.setProperty('--strength-width', '0%');
    elements.passwordStrengthLabel.textContent = '';
    elements.passwordStrengthLabel.style.color = '';
    elements.passwordLengthHint.textContent = '';
    elements.passwordMismatch.textContent = '';
    elements.currentPasswordGroup.classList.remove('error');
    elements.newPasswordGroup.classList.remove('error');
    elements.confirmPasswordGroup.classList.remove('error');
    elements.changePasswordMessage.textContent = '';
    elements.changePasswordMessage.classList.remove('success', 'error');
    elements.changePasswordSubmit.classList.remove('loading');
    updateSubmitButtonState();
}

// 关闭弹窗时重置回用户信息视图
function resetChangePasswordView() {
    hideChangePasswordView();
}

// 计算密码强度
function calculatePasswordStrength(password) {
    if (!password) {
        return { score: 0, label: '', width: '0%', color: 'transparent' };
    }
    if (password.length < 6) {
        return { score: 1, label: '弱', width: '25%', color: '#ef4444' };
    }
    let kinds = 0;
    if (/[a-z]/.test(password)) kinds++;
    if (/[A-Z]/.test(password)) kinds++;
    if (/[0-9]/.test(password)) kinds++;
    if (/[^a-zA-Z0-9]/.test(password)) kinds++;
    if (kinds <= 1 || password.length < 8) {
        return { score: 1, label: '弱', width: '40%', color: '#ef4444' };
    } else if (kinds === 2) {
        return { score: 2, label: '中', width: '70%', color: '#fb923c' };
    } else {
        return { score: 3, label: '强', width: '100%', color: '#00d4ff' };
    }
}

// 更新密码强度条 UI
function updatePasswordStrength() {
    const strength = calculatePasswordStrength(elements.newPassword.value);
    elements.passwordStrengthBar.style.setProperty('--strength-width', strength.width);
    elements.passwordStrengthBar.style.setProperty('--strength-color', strength.color);
    elements.passwordStrengthLabel.style.color = strength.color;
    elements.passwordStrengthLabel.textContent = strength.label;
    if (elements.newPassword.value && elements.newPassword.value.length < 6) {
        elements.passwordLengthHint.textContent = '密码至少 6 位';
        elements.passwordLengthHint.style.color = '#ef4444';
    } else {
        elements.passwordLengthHint.textContent = '';
    }
}

// 校验确认密码
function validateConfirmPassword() {
    if (!elements.confirmPassword.value) {
        elements.passwordMismatch.textContent = '';
        elements.confirmPasswordGroup.classList.remove('error');
        return;
    }
    if (elements.confirmPassword.value !== elements.newPassword.value) {
        elements.passwordMismatch.textContent = '两次密码不一致';
        elements.confirmPasswordGroup.classList.add('error');
    } else {
        elements.passwordMismatch.textContent = '';
        elements.confirmPasswordGroup.classList.remove('error');
    }
}

// 提交按钮禁用态联动
function updateSubmitButtonState() {
    const ready = elements.currentPassword.value &&
        elements.newPassword.value &&
        elements.confirmPassword.value &&
        elements.newPassword.value.length >= 6 &&
        elements.newPassword.value === elements.confirmPassword.value;
    elements.changePasswordSubmit.disabled = !ready;
}

// 密码显隐切换
function togglePasswordVisibility(targetId) {
    const input = document.getElementById(targetId);
    if (!input) return;
    const btn = document.querySelector(`.password-toggle-btn[data-target="${targetId}"]`);
    if (!btn) return;
    if (input.type === 'password') {
        input.type = 'text';
    } else {
        input.type = 'password';
    }
    const eyeOpen = btn.querySelector('.eye-open');
    const eyeClosed = btn.querySelector('.eye-closed');
    if (eyeOpen && eyeClosed) {
        if (input.type === 'text') {
            eyeOpen.style.display = 'none';
            eyeClosed.style.display = 'block';
        } else {
            eyeOpen.style.display = 'block';
            eyeClosed.style.display = 'none';
        }
    }
}

// 提交修改密码
async function handleChangePassword(e) {
    e.preventDefault();
    // 提交前再次校验
    if (!elements.currentPassword.value ||
        !elements.newPassword.value ||
        !elements.confirmPassword.value ||
        elements.newPassword.value.length < 6 ||
        elements.newPassword.value !== elements.confirmPassword.value) {
        return;
    }
    elements.changePasswordSubmit.classList.add('loading');
    elements.changePasswordSubmit.disabled = true;
    elements.changePasswordMessage.textContent = '';
    elements.changePasswordMessage.classList.remove('success', 'error');
    elements.currentPasswordGroup.classList.remove('error');
    try {
        const response = await fetch(`${API_BASE}/auth/change-password`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({
                current_password: elements.currentPassword.value,
                new_password: elements.newPassword.value
            })
        });
        if (response.ok) {
            elements.changePasswordMessage.textContent = '密码修改成功';
            elements.changePasswordMessage.classList.add('success');
            setTimeout(() => {
                hideChangePasswordView();
            }, 1500);
        } else if (response.status === 400) {
            const data = await response.json();
            if (data.detail && data.detail.includes('原密码')) {
                elements.currentPasswordGroup.classList.add('error');
                elements.changePasswordMessage.textContent = '原密码不正确';
            } else {
                elements.changePasswordMessage.textContent = data.detail || '修改失败';
            }
            elements.changePasswordMessage.classList.add('error');
        } else if (response.status === 401) {
            elements.changePasswordMessage.textContent = '登录已过期，请重新登录';
            elements.changePasswordMessage.classList.add('error');
        } else {
            elements.changePasswordMessage.textContent = '修改失败，请稍后重试';
            elements.changePasswordMessage.classList.add('error');
        }
    } catch (err) {
        elements.changePasswordMessage.textContent = '修改失败，请稍后重试';
        elements.changePasswordMessage.classList.add('error');
    } finally {
        elements.changePasswordSubmit.classList.remove('loading');
        updateSubmitButtonState();
    }
}

// 显示错误提示
function showError(message) {
    alert(message);
}

async function showAdminUsersModal() {
    if (!state.user || state.user.role !== 'admin') return;

    state.adminUserPage = 1;
    elements.adminUserSearchInput.value = '';
    state.adminUserSearchKeyword = '';
    elements.adminUsersModal.classList.add('active');
    await loadAdminUsers();
}

function hideAdminUsersModal() {
    elements.adminUsersModal.classList.remove('active');
}

function handleAdminUserSearch() {
    state.adminUserSearchKeyword = elements.adminUserSearchInput.value.trim();
    state.adminUserPage = 1;
    loadAdminUsers();
}

function changeAdminUsersPage(step) {
    const nextPage = state.adminUserPage + step;
    if (nextPage < 1 || nextPage > state.adminUserTotalPages) return;
    state.adminUserPage = nextPage;
    loadAdminUsers();
}

// ============ 日志查看（管理员） ============

async function showAdminLogsModal() {
    if (!state.user || state.user.role !== 'admin') return;

    state.adminLogsPage = 1;
    elements.adminLogsUsername.value = '';
    elements.adminLogsUserId.value = '';
    elements.adminLogsEventType.value = '';
    elements.adminLogsStartTime.value = '';
    elements.adminLogsEndTime.value = '';
    elements.adminLogsKeyword.value = '';
    state.adminLogsFilters = {
        user_id: '',
        username: '',
        event_type: '',
        start_time: '',
        end_time: '',
        keyword: ''
    };
    elements.adminLogsModal.classList.add('active');
    await loadAdminLogs();
}

function hideAdminLogsModal() {
    elements.adminLogsModal.classList.remove('active');
}

function handleAdminLogsSearch() {
    state.adminLogsFilters = {
        user_id: elements.adminLogsUserId.value.trim(),
        username: elements.adminLogsUsername.value.trim(),
        event_type: elements.adminLogsEventType.value,
        start_time: elements.adminLogsStartTime.value,
        end_time: elements.adminLogsEndTime.value,
        keyword: elements.adminLogsKeyword.value.trim()
    };
    state.adminLogsPage = 1;
    loadAdminLogs();
}

function changeAdminLogsPage(step) {
    const nextPage = state.adminLogsPage + step;
    if (nextPage < 1 || nextPage > state.adminLogsTotalPages) return;
    state.adminLogsPage = nextPage;
    loadAdminLogs();
}

async function loadAdminLogs() {
    if (!state.user || state.user.role !== 'admin') return;

    try {
        const params = new URLSearchParams({
            page: String(state.adminLogsPage),
            page_size: String(state.adminLogsPageSize)
        });
        const filters = state.adminLogsFilters || {};
        if (filters.user_id) params.set('user_id', filters.user_id);
        if (filters.username) params.set('username', filters.username);
        if (filters.event_type) params.set('event_type', filters.event_type);
        if (filters.start_time) params.set('start_time', filters.start_time);
        if (filters.end_time) params.set('end_time', filters.end_time);
        if (filters.keyword) params.set('keyword', filters.keyword);

        const response = await fetch(`${API_BASE}/admin/chat-logs?${params.toString()}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || '加载日志列表失败');
        }

        const data = await response.json();
        state.adminLogs = data.items || [];
        state.adminLogsPage = data.page || 1;
        state.adminLogsPageSize = data.page_size || state.adminLogsPageSize;
        state.adminLogsTotal = data.total || 0;
        state.adminLogsTotalPages = state.adminLogsPageSize > 0
            ? Math.max(1, Math.ceil(state.adminLogsTotal / state.adminLogsPageSize))
            : 1;

        renderAdminLogs();
    } catch (e) {
        console.error('Failed to load admin logs:', e);
        showError(e.message || '加载日志列表失败');
    }
}

function renderAdminLogs() {
    elements.adminLogsSummary.textContent = `共 ${state.adminLogsTotal} 条`;
    elements.adminLogsPageInfo.textContent = `第 ${state.adminLogsPage} / ${state.adminLogsTotalPages} 页`;
    elements.adminLogsPrevPageBtn.disabled = state.adminLogsPage <= 1;
    elements.adminLogsNextPageBtn.disabled = state.adminLogsPage >= state.adminLogsTotalPages;

    if (!state.adminLogs.length) {
        elements.adminLogsTableBody.innerHTML = '<tr><td colspan="5" class="empty-cell">暂无数据</td></tr>';
        return;
    }

    elements.adminLogsTableBody.innerHTML = state.adminLogs.map((item) => {
        const time = item.created_at ? formatDateTime(item.created_at) : '未知';
        const userDisplay = item.username || (item.user_id != null ? `#${item.user_id}` : '—');
        const eventType = item.event_type || '—';
        const content = item.content || '';
        const contextCount = item.context_count != null ? item.context_count : '—';
        return `
            <tr>
                <td>${escapeHTML(time)}</td>
                <td>${escapeHTML(String(userDisplay))}</td>
                <td>${escapeHTML(eventType)}</td>
                <td class="admin-logs-content-cell">${escapeHTML(content)}</td>
                <td>${escapeHTML(String(contextCount))}</td>
            </tr>
        `;
    }).join('');
}

async function loadAdminUsers() {
    if (!state.user || state.user.role !== 'admin') return;
    selectedUserIds.clear();

    try {
        const params = new URLSearchParams({
            page: String(state.adminUserPage),
            page_size: String(state.adminUserPageSize)
        });

        if (state.adminUserSearchKeyword) {
            params.set('keyword', state.adminUserSearchKeyword);
        }

        const response = await fetch(`${API_BASE}/admin/users?${params.toString()}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const contentType = response.headers.get('content-type') || '';
            if (contentType.includes('application/json')) {
                const error = await response.json();
                throw new Error(error.detail || '加载用户列表失败');
            }
            const text = (await response.text()).trim();
            throw new Error(text || `加载用户列表失败（HTTP ${response.status}）`);
        }

        const data = await response.json();
        state.adminUsers = data.users || [];
        state.adminUserPage = data.page || 1;
        state.adminUserTotalPages = data.total_pages || 1;
        state.adminUserTotal = data.total || 0;

        renderAdminUsers();
    } catch (e) {
        console.error('Failed to load admin users:', e);
        showError(e.message || '加载用户列表失败');
    }
}

// 批量选择状态
let selectedUserIds = new Set();

function updateBatchToolbar() {
    const toolbar = document.getElementById('adminBatchToolbar');
    const countEl = document.getElementById('adminBatchCount');
    if (selectedUserIds.size > 0) {
        toolbar.classList.add('active');
        countEl.textContent = `已选 ${selectedUserIds.size} 人`;
    } else {
        toolbar.classList.remove('active');
    }
}

function updateSelectAllState() {
    const selectAll = document.getElementById('adminSelectAllCheckbox');
    const rowCheckboxes = elements.adminUsersTableBody.querySelectorAll('.admin-row-checkbox:not(:disabled)');
    const checkedCount = Array.from(rowCheckboxes).filter(cb => cb.checked).length;
    selectAll.checked = rowCheckboxes.length > 0 && checkedCount === rowCheckboxes.length;
    selectAll.indeterminate = checkedCount > 0 && checkedCount < rowCheckboxes.length;

    // 更新 selectedUserIds
    selectedUserIds.clear();
    elements.adminUsersTableBody.querySelectorAll('.admin-row-checkbox:checked').forEach(cb => {
        selectedUserIds.add(parseInt(cb.dataset.userId));
    });
    updateBatchToolbar();
}

function clearSelection() {
    selectedUserIds.clear();
    const selectAll = document.getElementById('adminSelectAllCheckbox');
    if (selectAll) { selectAll.checked = false; selectAll.indeterminate = false; }
    elements.adminUsersTableBody.querySelectorAll('.admin-row-checkbox').forEach(cb => { cb.checked = false; });
    updateBatchToolbar();
}

function renderAdminUsers() {
    elements.adminUsersSummary.textContent = `共 ${state.adminUserTotal} 条`;
    elements.adminPageInfo.textContent = `第 ${state.adminUserPage} / ${state.adminUserTotalPages} 页`;
    elements.adminPrevPageBtn.disabled = state.adminUserPage <= 1;
    elements.adminNextPageBtn.disabled = state.adminUserPage >= state.adminUserTotalPages;

    if (!state.adminUsers.length) {
        elements.adminUsersTableBody.innerHTML = '<tr><td colspan="8" class="empty-cell">暂无数据</td></tr>';
        return;
    }

    elements.adminUsersTableBody.innerHTML = state.adminUsers.map((user) => {
        const createdAt = user.created_at ? new Date(user.created_at).toLocaleString('zh-CN') : '未知';
        const disableDelete = user.username === 'admin' || state.user.id === user.id;
        const isAdminRole = user.role === 'admin';
        const dailyLimit = isAdminRole ? '无限制' : (user.daily_limit || 10);
        const todayUsed = user.today_used || 0;
        const entity = describeAdminUserEntity(user);

        return `
            <tr>
                <td><input type="checkbox" class="admin-checkbox admin-row-checkbox" data-user-id="${escapeHTMLAttribute(user.id)}" ${isAdminRole ? 'disabled' : ''}></td>
                <td>${escapeHTML(user.username || '')}</td>
                <td>${escapeHTML(user.role || '')}</td>
                <td>${escapeHTML(createdAt)}</td>
                <td>${escapeHTML(dailyLimit)}</td>
                <td>${escapeHTML(todayUsed)}</td>
                <td class="admin-aic-entity-cell">
                    <span class="admin-aic-entity-status is-${escapeHTMLAttribute(entity.status)}">${escapeHTML(entity.label)}</span>
                    ${entity.detail ? `<small>${escapeHTML(entity.detail)}</small>` : ''}
                </td>
                <td>
                    <button class="admin-ensure-aic-entity-btn" data-user-id="${escapeHTMLAttribute(user.id)}" data-username="${escapeHTMLAttribute(user.username || '')}">${escapeHTML(entity.actionLabel)}</button>
                    ${isAdminRole ? '' : `
                        <button class="admin-edit-quota-btn" data-user-id="${escapeHTMLAttribute(user.id)}" data-username="${escapeHTMLAttribute(user.username || '')}" data-daily-limit="${escapeHTMLAttribute(user.daily_limit || 10)}" data-today-used="${escapeHTMLAttribute(todayUsed)}">
                            调整限额
                        </button>
                        <button class="admin-reset-quota-btn" data-user-id="${escapeHTMLAttribute(user.id)}" data-username="${escapeHTMLAttribute(user.username || '')}">
                            重置
                        </button>
                    `}
                    <button class="admin-delete-btn" data-user-id="${escapeHTMLAttribute(user.id)}" data-username="${escapeHTMLAttribute(user.username || '')}" ${disableDelete ? 'disabled' : ''}>
                        删除
                    </button>
                </td>
            </tr>
        `;
    }).join('');

    elements.adminUsersTableBody.querySelectorAll('.admin-delete-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            deleteAdminUser(btn.dataset.userId, btn.dataset.username);
        });
    });

    elements.adminUsersTableBody.querySelectorAll('.admin-edit-quota-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            showEditQuotaModal(
                parseInt(btn.dataset.userId),
                btn.dataset.username,
                parseInt(btn.dataset.dailyLimit),
                parseInt(btn.dataset.todayUsed)
            );
        });
    });

    elements.adminUsersTableBody.querySelectorAll('.admin-reset-quota-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            resetAdminUserQuota(parseInt(btn.dataset.userId), btn.dataset.username);
        });
    });

    elements.adminUsersTableBody.querySelectorAll('.admin-ensure-aic-entity-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            ensureAdminUserAicEntity(btn.dataset.userId, btn.dataset.username);
        });
    });

    // 复选框事件
    const rowCheckboxes = elements.adminUsersTableBody.querySelectorAll('.admin-row-checkbox');
    rowCheckboxes.forEach((cb) => {
        cb.addEventListener('change', () => {
            updateBatchToolbar();
            updateSelectAllState();
        });
    });

    // 初始化全选框状态
    updateSelectAllState();
}

function describeAdminUserEntity(user) {
    const status = user.aip_entity_status || 'missing';
    const labels = {
        active: '有效',
        expiring: '临近到期',
        expired: '证书已过期',
        error: '申请或续期失败',
        missing: '未申请'
    };
    const expiry = user.aip_entity_expires_at
        ? `证书至 ${new Date(user.aip_entity_expires_at).toLocaleDateString('zh-CN')}`
        : '';
    const detail = [user.aip_entity_aic || '', expiry].filter(Boolean).join(' · ');
    return {
        status,
        label: labels[status] || '状态未知',
        detail,
        actionLabel: status === 'missing' ? '申请 AIC' : '续期证书'
    };
}

async function ensureAdminUserAicEntity(userId, username) {
    if (!confirm(`确认帮助用户 ${username} 申请专属 AIC 实体或续期证书吗？`)) return;
    try {
        const response = await fetch(`${API_BASE}/admin/users/${userId}/aic-entity/ensure`, {
            method: 'POST',
            headers: getAuthHeaders()
        });
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || `AIC 实体操作失败（HTTP ${response.status}）`);
        }
        const result = await response.json();
        alert(result.message);
        await loadAdminUsers();
    } catch (error) {
        console.error('Failed to ensure user AIC entity:', error);
        showError(error.message || 'AIC 实体申请或证书续期失败');
    }
}

async function deleteAdminUser(userId, username) {
    if (!confirm(`确认删除用户 ${username} 吗？\n此操作会删除该用户的聊天记录、日志等所有相关数据，且不可恢复。`)) {
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/admin/users/${userId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '删除用户失败');
        }

        alert(`用户 ${username} 删除成功`);
        await loadAdminUsers();
    } catch (e) {
        console.error('Failed to delete user:', e);
        showError(e.message || '删除用户失败');
    }
}

let currentEditQuotaUserId = null;

function showEditQuotaModal(userId, username, dailyLimit, todayUsed) {
    currentEditQuotaUserId = userId;
    document.getElementById('editQuotaUsername').value = username;
    document.getElementById('editQuotaDailyLimit').value = dailyLimit;
    document.getElementById('editQuotaTodayUsed').value = todayUsed;
    document.getElementById('editQuotaModal').classList.add('active');
}

function hideEditQuotaModal() {
    document.getElementById('editQuotaModal').classList.remove('active');
    currentEditQuotaUserId = null;
}

async function saveEditQuota() {
    if (!currentEditQuotaUserId) return;

    const dailyLimit = parseInt(document.getElementById('editQuotaDailyLimit').value);
    if (isNaN(dailyLimit) || dailyLimit < 0) {
        alert('请输入有效的限额（大于等于0）');
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/admin/users/${currentEditQuotaUserId}/quota`, {
            method: 'PUT',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ daily_limit: dailyLimit })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '调整限额失败');
        }

        alert('限额调整成功');
        hideEditQuotaModal();
        await loadAdminUsers();
    } catch (e) {
        console.error('Failed to update quota:', e);
        showError(e.message || '调整限额失败');
    }
}

async function resetAdminUserQuota(userId, username) {
    if (!confirm(`确认重置用户 ${username} 的今日使用次数吗？`)) {
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/admin/users/${userId}/quota/reset`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '重置限额失败');
        }

        alert(`用户 ${username} 的今日使用次数已重置`);
        await loadAdminUsers();
    } catch (e) {
        console.error('Failed to reset quota:', e);
        showError(e.message || '重置限额失败');
    }
}

async function batchResetQuota() {
    if (selectedUserIds.size === 0) return;
    if (!confirm(`确认重置选中的 ${selectedUserIds.size} 个用户的今日使用次数吗？`)) return;

    try {
        const response = await fetch(`${API_BASE}/admin/users/batch-quota-reset`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_ids: Array.from(selectedUserIds) })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '批量重置失败');
        }

        const result = await response.json();
        alert(result.message);
        clearSelection();
        await loadAdminUsers();
    } catch (e) {
        console.error('Failed to batch reset:', e);
        showError(e.message || '批量重置失败');
    }
}

async function batchEnsureAdminUserAicEntities() {
    if (selectedUserIds.size === 0) return;
    if (!confirm(`确认帮助选中的 ${selectedUserIds.size} 个用户申请专属 AIC 实体或续期证书吗？`)) return;
    try {
        const response = await fetch(`${API_BASE}/admin/users/batch-aic-entity/ensure`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_ids: Array.from(selectedUserIds) })
        });
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || '批量申请或续期 AIC 失败');
        }
        const result = await response.json();
        const failed = result.failed_usernames?.length ? `\n失败用户：${result.failed_usernames.join('、')}` : '';
        alert(`${result.message}${failed}`);
        clearSelection();
        await loadAdminUsers();
    } catch (error) {
        console.error('Failed to batch ensure user AIC entities:', error);
        showError(error.message || '批量申请或续期 AIC 失败');
    }
}

async function batchUpdateQuota() {
    if (selectedUserIds.size === 0) return;
    document.getElementById('batchQuotaUserCount').value = `${selectedUserIds.size} 人`;
    document.getElementById('batchQuotaDailyLimit').value = '';
    document.getElementById('batchQuotaModal').classList.add('active');
}

function hideBatchQuotaModal() {
    document.getElementById('batchQuotaModal').classList.remove('active');
}

async function saveBatchQuota() {
    const dailyLimit = parseInt(document.getElementById('batchQuotaDailyLimit').value);
    if (isNaN(dailyLimit) || dailyLimit < 0) {
        alert('请输入有效的限额（大于等于0）');
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/admin/users/batch-quota`, {
            method: 'PUT',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_ids: Array.from(selectedUserIds), daily_limit: dailyLimit })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '批量调整限额失败');
        }

        const result = await response.json();
        alert(result.message);
        hideBatchQuotaModal();
        clearSelection();
        await loadAdminUsers();
    } catch (e) {
        console.error('Failed to batch update quota:', e);
        showError(e.message || '批量调整限额失败');
    }
}

// ============================================
// 发现页交互逻辑
// ============================================

const categoryAgents = {
    tools: [
        { icon: '🤖', name: '智能写作助手', desc: '帮你快速生成高质量的文章和文案' },
        { icon: '💻', name: '代码助手', desc: '智能编程助手，提升开发效率' },
        { icon: '📝', name: '笔记整理', desc: '智能整理笔记，提炼核心要点' },
        { icon: '🔍', name: '信息检索', desc: '快速查找和整理各类信息' }
    ],
    learning: [
        { icon: '📚', name: '学习规划师', desc: '定制个性化学习计划和路径' },
        { icon: '🎯', name: '刷题助手', desc: '智能出题，针对性练习提升' },
        { icon: '🧠', name: '知识问答', desc: '解答各类学习疑问' },
        { icon: '📖', name: '读书笔记', desc: '智能提炼书籍精华内容' }
    ],
    entertainment: [
        { icon: '🎮', name: '游戏攻略', desc: '提供各类游戏攻略和技巧' },
        { icon: '🎵', name: '音乐推荐', desc: '根据口味推荐个性化音乐' },
        { icon: '🎬', name: '影视推荐', desc: '精选电影和电视剧推荐' },
        { icon: '🎨', name: '创意涂鸦', desc: '激发创意，生成有趣的文字绘画' }
    ],
    office: [
        { icon: '📊', name: '数据分析专家', desc: '轻松处理复杂数据，生成可视化报告' },
        { icon: '📋', name: 'PPT 助手', desc: '快速生成演示文稿大纲和内容' },
        { icon: '📧', name: '邮件撰写', desc: '专业高效的邮件写作助手' },
        { icon: '📁', name: '文档整理', desc: '智能分类和整理办公文档' }
    ]
};

function handleDiscoverSearch() {
    const keyword = elements.discoverSearchInput.value.trim();
    if (!keyword) {
        alert('请输入搜索关键词');
        return;
    }

    if (!state.user) {
        showAuthModal();
        return;
    }

    switchTab('chat');
    
    setTimeout(() => {
        elements.messageInput.value = `帮我找一个关于"${keyword}"的智能体`;
        elements.messageInput.focus();
    }, 100);
}

function handleCategoryClick(category) {
    elements.categoryTags.forEach(tag => {
        tag.classList.toggle('active', tag.dataset.category === category);
    });

    const agents = categoryAgents[category] || [];
    renderDiscoverAgents(agents);
}

function renderDiscoverAgents(agents) {
    if (!elements.discoverAgentsGrid) return;

    elements.discoverAgentsGrid.innerHTML = agents.map(agent => `
        <div class="discover-agent-card glass-card" data-agent-name="${escapeHTMLAttribute(agent.name)}">
            <div class="agent-card-icon">${agent.icon}</div>
            <div class="agent-card-info">
                <div class="agent-card-name">${escapeHTML(agent.name)}</div>
                <div class="agent-card-desc">${escapeHTML(agent.desc)}</div>
            </div>
        </div>
    `).join('');
}

function handleAgentCardClick(card) {
    const agentName = card.dataset.agentName;
    if (!agentName) return;

    if (!state.user) {
        showAuthModal();
        return;
    }

    switchTab('chat');
    
    setTimeout(() => {
        elements.messageInput.value = `我想使用「${agentName}」智能体`;
        elements.messageInput.focus();
    }, 100);
}

function handleDiscoverMore() {
    if (!state.user) {
        showAuthModal();
        return;
    }

    switchTab('chat');
    
    setTimeout(() => {
        exploreAgents();
    }, 100);
}

// ============================================
// 我的页交互逻辑
// ============================================

function handleMenuSettings() {
    if (!state.user) {
        showAuthModal();
        return;
    }
    showIoaUsernameSettings();
}

function showIoaUsernameSettings() {
    var currentIoaUsername = (state.user && state.user.ioa_username) ? state.user.ioa_username : '';
    var html = '<div class="settings-modal" style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;">';
    html += '<div style="background:#1a1a2e;border-radius:1rem;padding:1.5rem;width:90%;max-width:25rem;box-shadow:0 0.5rem 2rem rgba(0,0,0,0.4);">';
    html += '<h3 style="color:#e0e0e0;margin:0 0 0.5rem 0;font-size:1.125rem;">\u8bbe\u7f6e</h3>';
    html += '<p style="color:#888;margin:0 0 1.25rem 0;font-size:0.8125rem;">\u8bbe\u7f6e\u4f60\u7684 IOA \u7528\u6237\u540d\uff0c\u4f18\u5148\u5c55\u793a\u5339\u914d\u7684\u667a\u80fd\u4f53\u3002</p>';
    html += '<label style="color:#ccc;font-size:0.8125rem;display:block;margin-bottom:0.375rem;">IOA \u7528\u6237\u540d</label>';
    html += '<input id="ioa-username-input" type="text" value="' + escapeHtml(currentIoaUsername) + '" placeholder="\u8bf7\u8f93\u5165\u4f60\u7684 IOA \u7528\u6237\u540d" style="width:100%;padding:0.625rem 0.75rem;border:1px solid #333;border-radius:0.5rem;background:#12121f;color:#e0e0e0;font-size:0.875rem;box-sizing:border-box;outline:none;">';
    html += '<div style="margin-top:1.25rem;display:flex;gap:0.625rem;justify-content:flex-end;">';
    html += '<button id="ioa-username-cancel" style="padding:0.5rem 1rem;border:1px solid #444;border-radius:0.5rem;background:transparent;color:#aaa;cursor:pointer;font-size:0.8125rem;">\u53d6\u6d88</button>';
    html += '<button id="ioa-username-save" style="padding:0.5rem 1.25rem;border:none;border-radius:0.5rem;background:#4a6cf7;color:#fff;cursor:pointer;font-size:0.8125rem;">\u4fdd\u5b58</button>';
    html += '</div></div></div>';

    var container = document.createElement('div');
    container.id = 'ioa-settings-container';
    container.innerHTML = html;
    document.body.appendChild(container);

    document.getElementById('ioa-username-cancel').addEventListener('click', function() {
        document.body.removeChild(container);
    });

    document.getElementById('ioa-username-save').addEventListener('click', async function() {
        var input = document.getElementById('ioa-username-input');
        var newValue = input.value.trim();
        try {
            var response = await fetch(API_BASE + '/auth/ioa-username', {
                method: 'PUT',
                headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                body: JSON.stringify({ ioa_username: newValue || null })
            });
            if (response.ok) {
                state.user = await response.json();
                document.body.removeChild(container);
            } else {
                alert('\u4fdd\u5b58\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5');
            }
        } catch (e) {
            console.error('Save IOA username error:', e);
            alert('\u4fdd\u5b58\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5');
        }
    });

    container.addEventListener('click', function(e) {
        if (e.target === container) {
            document.body.removeChild(container);
        }
    });
}

function handleMenuAbout() {
    alert('叮当 AI Platform\n版本：1.8.2\n\n你的智能体浏览器助手');
}

function handleProfileLogout() {
    if (!state.user) {
        showAuthModal();
        return;
    }
    if (confirm('确定要退出登录吗？')) {
        logout();
    }
}

// ============================================
// 全局 Dingdang 对象 - 供父网站通过 iframe 调用
// ============================================
window.Dingdang = window.Dingdang || {};

// 父网站可调用此函数，在 iframe 内触发叮当已有的智能体调用流程。
window.Dingdang.invokeAgent = async function(agent, task) {
    if (!agent || !agent.name) {
        console.warn('[Dingdang] invokeAgent: agent 信息不完整');
        return { ok: false, error: 'agent 信息不完整' };
    }

    if (!state.user) {
        showAuthModal();
        return { ok: false, error: '请先登录' };
    }

    try {
        return await invokeEmbeddedAgent(agent, task);
    } catch (error) {
        console.error('[Dingdang] invokeAgent failed:', error);
        return { ok: false, error: error.message };
    }
};

// 生成单成员协作确认卡 HTML，供需要在叮当内部嵌入 Agent 控件的页面复用。
function createSingleAgentCardHTML(agent, task) {
    const icon = agent.type === 'web' ? '🌐' : '🔧';
    const typeLabel = agent.type === 'web' ? 'Web 应用' : '服务调用';
    const description = UI_HELPERS.getAgentDescriptionForDisplay
        ? UI_HELPERS.getAgentDescriptionForDisplay(agent.name, agent.description)
        : String(agent.description || '').trim();
    const meta = [description, typeLabel].filter(Boolean).join(' · ');
    let bodyHTML = '';

    const endpointError = String(agent.endpoint_error || '').trim();
    const isCallable = agent.callable !== false && !endpointError;
    if (agent.type === 'web') {
        bodyHTML = '<div class="agent-card-actions">' +
            '<button class="btn-cancel-agent-invoke" type="button">暂不调用</button>' +
            '<button class="btn-open-web-agent" type="button">确认并打开</button>' +
            '</div>';
    } else if (!isCallable) {
        bodyHTML = '<div class="agent-card-actions">' +
            '<button class="btn-cancel-agent-invoke" type="button">关闭</button>' +
            '<button class="btn-invoke-agent" type="button" disabled title="' + escapeHTMLAttribute(endpointError) + '">端点不可调用</button>' +
            '</div><div class="agent-endpoint-error">' + escapeHTML(endpointError || '智能体注册端点不可从当前服务端访问') + '</div>';
    } else {
        bodyHTML = '<textarea class="agent-task-input" ' +
            'aria-label="发送给智能体的指令" placeholder="请输入要交给智能体的指令..." ' +
            'rows="2">' + (task ? escapeHTML(task) : '') + '</textarea>' +
            '<div class="agent-card-actions">' +
            '<button class="btn-cancel-agent-invoke" type="button">暂不调用</button>' +
            '<button class="btn-invoke-agent" type="button">确认并开始协作</button>' +
            '</div>';
    }

    return '<div class="single-agent-card glass-card">' +
        '<div class="agent-call-confirmation-kicker"><span>✦ 叮当协作确认</span><small>本轮将由叮当协调 1 个智能体，可确认或修改指令</small></div>' +
        '<div class="agent-card-header">' +
            '<div class="agent-card-icon">' + icon + '</div>' +
            '<div class="agent-card-info">' +
                '<div class="agent-card-name">' + escapeHTML(agent.name) + '</div>' +
                '<div class="agent-card-desc">' + escapeHTML(meta) + '</div>' +
            '</div>' +
        '</div>' +
        '<div class="agent-card-body">' + bodyHTML + '</div>' +
        '<div class="agent-result" style="display:none;">' +
            '<div class="result-label">执行结果</div>' +
            '<div class="result-content"></div>' +
        '</div>' +
        '<div class="agent-status" style="display:none;">' +
            '<span class="status-dot"></span>' +
            '<span class="status-text">正在调用...</span>' +
        '</div>' +
    '</div>';
}

// 处理单成员协作卡片动作；即使只有一个成员，也必须进入 /agent/execute
// 的 Dingdang collaboration DAG，不能再分叉到 direct-invoke。
async function handleSingleAgentInvoke(cardEl, agent) {
    if (state.isLoading) return;

    const inputEl = cardEl.querySelector('.agent-task-input');
    const task = inputEl ? inputEl.value.trim() : '';
    if (!task) {
        alert('请输入指令');
        return;
    }

    const invokeBtn = cardEl.querySelector('.btn-invoke-agent');
    const statusEl = cardEl.querySelector('.agent-status');
    const resultEl = cardEl.querySelector('.agent-result');
    const resultContent = resultEl?.querySelector('.result-content');

    if (invokeBtn) {
        invokeBtn.disabled = true;
        invokeBtn.textContent = '调用中...';
    }
    if (statusEl) {
        statusEl.style.display = 'flex';
        statusEl.className = 'agent-status';
        statusEl.querySelector('.status-text').textContent = '正在调用 ' + escapeHTML(agent.name) + '...';
    }
    if (resultEl) {
        resultEl.style.display = 'none';
    }
    if (inputEl) {
        inputEl.disabled = true;
    }

    state.isLoading = true;
    let submitted = false;
    try {
        const response = await fetch(API_BASE + '/agent/execute', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...getAuthHeaders()
            },
            // 嵌入确认卡可能来自尚未持久化的草稿；后端会为这次单成员协作
            // 创建真实会话，并在响应头中返回它。
            body: JSON.stringify({
                selected_agents: [agent.name],
                ioa_agents: [{ ...agent, assigned_task: task }],
                user_query: task,
                conversation_id: state.currentConversationId || null
            })
        });

        if (!response.ok) {
            throw new Error('调用失败 (HTTP ' + response.status + ')');
        }

        applyQuotaHeaders(response);
        // 协作接口会为草稿创建真实会话，并在响应头中先返回它。必须在读 SSE
        // 前认领该 ID，否则事件会被当前会话过滤逻辑误判为后台会话，直到历史回放。
        const resolvedConversationId = response.headers.get('X-Conversation-ID') || state.currentConversationId;
        if (resolvedConversationId) {
            adoptConversationId(resolvedConversationId);
        }
        // 与叮当内部发现路径共用 SSE 处理器，确保 A2UI 卡片、awaiting-input
        // 和主输入框多轮状态在两种入口下完全一致。
        await processExecutionStream(response, resolvedConversationId);
        submitted = true;
    } catch (e) {
        console.error('[Dingdang] 智能体调用失败:', e);
        if (statusEl) {
            statusEl.className = 'agent-status error';
            statusEl.querySelector('.status-text').textContent = '调用失败: ' + e.message;
        }
        if (resultEl && resultContent) {
            resultEl.style.display = 'block';
            resultContent.innerHTML = '<span style="color: #f87171;">' + escapeHTML(e.message) + '</span>';
        }
    } finally {
        state.isLoading = false;
        if (invokeBtn) {
            invokeBtn.disabled = false;
            invokeBtn.textContent = '确认并开始协作';
        }
        if (inputEl) {
            inputEl.disabled = false;
        }
        if (submitted) {
            cardEl.closest('.embedded-agent-confirmation')?.remove();
        }
    }
    scrollToBottom();
}

// DOM-backed behavior contract used by the served shell and focused regression tests.
window.DingdangUiBehavior = Object.freeze({
    initializeTheme,
    setThemePreference,
    setMobileSidebarOpen,
    renderInlineAgentDiscovery,
    presentAgentResult,
    setInlineExecutionStatus,
    getDynamicActionPayloadCount: () => dynamicActionPayloads.size
});

let developerDebugSessionId = '';
let developerDebugEvents = [];
let developerDebugRunning = false;
let developerDebugCanContinue = false;

const developerTemplateMessages = {
    start: JSON.stringify({query: '{任务目标}'}, null, 2),
    continue: JSON.stringify({query: '{后续问题或补充要求}'}, null, 2),
    cancel: JSON.stringify({query: '取消当前任务'}, null, 2),
    supplement: JSON.stringify({query: '{补充信息}'}, null, 2),
};

const developerAcsTemplates = {
    discovery: JSON.stringify({
        aic: '1.2.156.3088.1.0001.00001.Z2L0JR.YLE7QO.0QD0',
        name: '智能体发现Agent',
        endPoints: [{
            url: 'https://192.0.2.20:9988/acps/aip/v1/rpc',
            transport: 'JSONRPC',
        }],
        protocolVersion: '02.01',
        defaultInputModes: ['application/json'],
        defaultOutputModes: ['application/json'],
    }, null, 2),
};

function setDeveloperDebugStatus(message) {
    if (elements.developerDebugStatus) {
        elements.developerDebugStatus.textContent = message;
    }
}

function renderDeveloperDebugEvents() {
    if (elements.developerRawResponse) {
        elements.developerRawResponse.textContent = developerDebugEvents
            .map(event => JSON.stringify(event, null, 2))
            .join('\n\n');
    }
}

function setDeveloperDebugRunning(running) {
    developerDebugRunning = running;
    if (elements.developerSendBtn) elements.developerSendBtn.disabled = running;
}

async function developerDebugStream(url, payload) {
    setDeveloperDebugRunning(true);
    setDeveloperDebugStatus('正在发送消息…');
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json', ...getAuthHeaders()},
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            const errorBody = await response.text();
            let errorMessage = '';
            try {
                const error = JSON.parse(errorBody);
                const detail = error.detail || error.message;
                errorMessage = typeof detail === 'string' ? detail : JSON.stringify(detail);
            } catch (_error) {
                errorMessage = errorBody;
            }
            developerDebugEvents.push({
                type: "debug_response_error",
                http_status: response.status,
                raw_response: errorBody,
            });
            renderDeveloperDebugEvents();
            throw new Error(errorMessage || `调试请求失败（HTTP ${response.status}）`);
        }

        setDeveloperDebugStatus('消息已发送，等待智能体响应…');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let pending = '';
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            pending += decoder.decode(value, {stream: true});
            let separator;
            while ((separator = pending.indexOf('\n\n')) >= 0) {
                const block = pending.slice(0, separator);
                pending = pending.slice(separator + 2);
                const line = block.split('\n').find(item => item.startsWith('data: '));
                if (!line) continue;
                const event = JSON.parse(line.slice(6));
                developerDebugSessionId = event.debug_session_id || developerDebugSessionId;
                developerDebugEvents.push(event);
                renderDeveloperDebugEvents();
                const state = event.error ? '调用失败' : (event.agent_result?.state || event.status || '运行中');
                developerDebugCanContinue = state === 'awaiting-input';
                setDeveloperDebugStatus(`状态：${state}`);
            }
        }
    } finally {
        setDeveloperDebugRunning(false);
    }
}

async function loadDeveloperDebugLogs() {
    const response = await fetch(`${API_BASE}/developer-tools/debug/logs`, {headers: getAuthHeaders()});
    if (!response.ok) throw new Error('调试日志加载失败');
    elements.developerLogsOutput.textContent = JSON.stringify(await response.json(), null, 2);
}

function setDeveloperToolsTab(tab) {
    const logs = tab === 'logs';
    elements.developerDebugPanel.hidden = logs;
    elements.developerLogsPanel.hidden = !logs;
    document.querySelectorAll('[data-debug-tab]').forEach(button => {
        const selected = button.dataset.debugTab === tab;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', String(selected));
    });
}

function setupDeveloperTools() {
    elements.sidebarDeveloperTools?.addEventListener('click', () => {
        closeSidebarAccountMenu();
        elements.developerToolsModal.classList.add('active');
    });
    elements.closeDeveloperToolsModal?.addEventListener('click', () => elements.developerToolsModal.classList.remove('active'));
    elements.developerSendBtn?.addEventListener('click', async () => {
        const message = elements.developerMessageInput.value;
        if (!message.trim()) {
            setDeveloperDebugStatus('请输入消息。');
            return;
        }
        const continuing = developerDebugSessionId && developerDebugCanContinue;
        if (!continuing) {
            developerDebugSessionId = '';
            developerDebugCanContinue = false;
            developerDebugEvents = [];
            renderDeveloperDebugEvents();
        }
        try {
            await developerDebugStream(
                continuing
                    ? `${API_BASE}/developer-tools/debug/sessions/${encodeURIComponent(developerDebugSessionId)}/messages`
                    : `${API_BASE}/developer-tools/debug/sessions`,
                continuing
                    ? {message}
                    : {acs_text: elements.developerAcsInput.value, message},
            );
        } catch (error) {
            setDeveloperDebugStatus(`发送失败：${error.message}`);
        }
    });
    document.querySelectorAll('[data-developer-message-template]').forEach(button => button.addEventListener('click', () => {
        const template = developerTemplateMessages[button.dataset.developerMessageTemplate];
        if (!template) return;
        elements.developerMessageInput.value = template;
        elements.developerMessageInput.focus();
    }));
    document.querySelectorAll('[data-developer-acs-template]').forEach(button => button.addEventListener('click', () => {
        const template = developerAcsTemplates[button.dataset.developerAcsTemplate];
        if (!template) return;
        elements.developerAcsInput.value = template;
        elements.developerAcsInput.focus();
    }));
    elements.developerClearBtn?.addEventListener('click', () => {
        developerDebugEvents = [];
        renderDeveloperDebugEvents();
        setDeveloperDebugStatus('已仅清空当前浏览器中的展示。');
    });
    document.querySelectorAll('[data-debug-tab]').forEach(button => button.addEventListener('click', async () => {
        const tab = button.dataset.debugTab;
        setDeveloperToolsTab(tab);
        if (tab === 'logs') {
            try {
                await loadDeveloperDebugLogs();
            } catch (error) {
                elements.developerLogsOutput.textContent = error.message;
            }
        }
    }));
    setDeveloperDebugRunning(false);
}
setupDeveloperTools();

// 启动应用
init();
