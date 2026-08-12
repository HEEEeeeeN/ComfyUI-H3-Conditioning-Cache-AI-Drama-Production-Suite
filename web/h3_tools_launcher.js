import { app } from "../../scripts/app.js";

// 在 ComfyUI 顶部菜单栏注册一个"懒"按钮，点击后通过后端路由启动 AI剧生产套件 GUI。
// 策略1: 使用 window.comfyAPI 公开 API 创建菜单按钮（新版前端）
// 策略2: DOM 回退——直接在顶部菜单栏插入按钮（兼容旧版/魔改前端）
app.registerExtension({
    name: "H3Tools.Launcher",
    async setup() {
        const MAX_ATTEMPTS = 40; // 最多等 20 秒（每 500ms 一次）
        let attempts = 0;
        let added = false;

        const launchGUI = async () => {
            try {
                const resp = await fetch("/h3_tools/launch", { cache: "no-store" });
                const data = await resp.json();
                if (!resp.ok) {
                    alert("启动失败: " + (data?.error || resp.statusText));
                }
            } catch (e) {
                alert("启动失败: " + e);
            }
        };

        // ── 策略1: comfyAPI 按钮 ──
        const tryComfyAPI = () => {
            const { ComfyButton } = window.comfyAPI?.button ?? {};
            const { ComfyButtonGroup } = window.comfyAPI?.buttonGroup ?? {};
            if (!ComfyButton || !ComfyButtonGroup) return false;

            try {
                const lazyIcon = document.createElement("span");
                lazyIcon.textContent = "懒";
                lazyIcon.style.cssText =
                    "font-size:16px;line-height:1;font-weight:700;font-family:'Microsoft YaHei UI','PingFang SC',sans-serif;";

                const launchButton = new ComfyButton({
                    content: lazyIcon,
                    tooltip: "打开 AI 剧生产套件（懒）",
                    action: launchGUI,
                    classList: "comfyui-button primary",
                });

                const group = new ComfyButtonGroup(launchButton);
                const settingsGroup = app.menu?.settingsGroup;
                if (settingsGroup?.element) {
                    settingsGroup.element.before(group.element);
                    console.log("[H3Tools] 懒按钮已通过 comfyAPI 添加");
                    return true;
                }
            } catch (e) {
                console.warn("[H3Tools] comfyAPI 按钮创建失败:", e);
            }
            return false;
        };

        // ── 策略2: DOM 回退 ──
        const tryDOMFallback = () => {
            // 尝试多种选择器找到顶部菜单栏
            const selectors = [
                ".comfyui-menu",
                ".comfy-menu",
                "[class*='topbar']",
                "[class*='menu-bar']",
                ".p-menubar",
                "header",
            ];

            for (const sel of selectors) {
                const menu = document.querySelector(sel);
                if (!menu) continue;

                // 避免重复添加
                if (menu.querySelector("#h3-lazy-btn")) return true;

                const btn = document.createElement("button");
                btn.id = "h3-lazy-btn";
                btn.textContent = "懒";
                btn.title = "打开 AI 剧生产套件（懒）";
                btn.style.cssText = [
                    "font-size:16px",
                    "font-weight:700",
                    "font-family:'Microsoft YaHei UI','PingFang SC',sans-serif",
                    "padding:4px 10px",
                    "margin:0 2px",
                    "border:1px solid var(--border-color,#3a3a44)",
                    "border-radius:5px",
                    "background:var(--p-primary-color,#4a90d9)",
                    "color:#fff",
                    "cursor:pointer",
                    "line-height:1",
                    "min-height:28px",
                ].join(";");

                btn.addEventListener("click", launchGUI);
                menu.appendChild(btn);
                console.log("[H3Tools] 懒按钮已通过 DOM 回退添加到:", sel);
                return true;
            }
            return false;
        };

        const tryAdd = () => {
            if (added) return;

            // 先试 comfyAPI，再试 DOM 回退
            if (tryComfyAPI() || tryDOMFallback()) {
                added = true;
                return;
            }

            if (attempts++ < MAX_ATTEMPTS) {
                setTimeout(tryAdd, 500);
            } else {
                console.warn("[H3Tools] 无法添加懒按钮（菜单栏未找到）");
            }
        };

        tryAdd();
    },
});
