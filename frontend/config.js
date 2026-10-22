// 切换为 backend 后，所有数据和执行请求均通过 api.js 发送到后端。
export const DEFAULT_CONFIG = Object.freeze({
  mode: "backend",
  apiBaseUrl: "http://127.0.0.1:8080/api/v1",
  sceneUrl: "./assets/scene.json",
});

export function loadConfig(storage = globalThis.localStorage) {
  try {
    const saved = JSON.parse(storage.getItem("panda.frontend.settings") || "{}");
    return { ...DEFAULT_CONFIG, ...saved };
  } catch {
    return { ...DEFAULT_CONFIG };
  }
}

// 一体化启动器提供配置，覆盖旧浏览器中保存的静态预览设置。
export async function loadRuntimeConfig(config, fetcher = globalThis.fetch, baseUrl = globalThis.location.href) {
  const response = await fetcher("./studio-config.json", { cache: "no-store" });
  if (response.status === 404) return config;
  if (!response.ok) throw new Error("无法读取仿真启动配置，请重启网页服务。");
  const runtime = await response.json();
  return { ...config, ...runtime, apiBaseUrl: new URL(runtime.apiBaseUrl || config.apiBaseUrl, baseUrl).href };
}
