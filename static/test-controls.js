export async function readTestEvents(response, onEvent) {
  if (!response.ok) {
    const payload = await response.json();
    throw new Error(payload.error || "接口请求失败");
  }
  if (!response.body || !response.headers.get("Content-Type")?.includes("application/x-ndjson")) {
    throw new Error("测试接口未返回预期的进度数据");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      let newline;
      while ((newline = pending.indexOf("\n")) !== -1) {
        const line = pending.slice(0, newline).trim();
        pending = pending.slice(newline + 1);
        if (!line) continue;
        const packet = JSON.parse(line);
        if (packet.type === "error") throw new Error(packet.error || "流式请求失败");
        onEvent(packet);
        if (packet.type === "result") return packet;
      }
      if (done) throw new Error("连接中断，未收到完整测试结果，本次回答不计入");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
