import { app } from "../../scripts/app.js";

// 给 H3LoadConditioning 节点添加"浏览 .pt 文件"按钮。
// 点击后调用后端 POST /h3cache/pick_pt，用系统原生文件对话框选择任意位置的 .pt
// 文件（不再锁死在缓存目录 combo 列表里），选中后把绝对路径填入 file_name widget。
app.registerExtension({
    name: "H3Cache.LoadPTFile",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "H3LoadConditioning") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);

            // 找到 file_name combo widget
            const fileWidget = this.widgets?.find((w) => w.name === "file_name");
            if (!fileWidget) return r;

            const pickAndSet = async () => {
                try {
                    const resp = await fetch("/h3cache/pick_pt", { method: "POST" });
                    const data = await resp.json();
                    if (!resp.ok || data.error) {
                        alert("选择失败: " + (data?.error || resp.statusText));
                        return;
                    }
                    const path = data.path;
                    if (!path) return; // 用户取消

                    // 把绝对路径加入 combo 候选并选中
                    const opts = fileWidget.options;
                    if (opts) {
                        if (!opts.values) opts.values = [];
                        if (!opts.values.includes(path)) opts.values.push(path);
                        // 新版 vue combo 可能需要重新触发重绘
                        if (typeof opts.update === "function") opts.update();
                    }
                    fileWidget.value = path;
                    if (typeof fileWidget.callback === "function") {
                        fileWidget.callback(path);
                    }
                    app.graph?.setDirtyCanvas(true, false);
                } catch (e) {
                    alert("选择失败: " + e);
                }
            };

            // 添加按钮 widget（与 LoadImage 的 choose file to upload 类似）
            const btn = this.addWidget("button", "浏览 .pt 文件", null, pickAndSet);
            btn.serialize = false; // 按钮不需要保存到工作流 JSON

            // 兼容旧版 litegraph：combo 值可能被 options 校验，确保自定义路径可保存
            const origSerialize = fileWidget.serializeValue?.bind?.(fileWidget);
            if (origSerialize) {
                fileWidget.serializeValue = function (...args) {
                    return this.value;
                };
            }

            return r;
        };
    },
});
