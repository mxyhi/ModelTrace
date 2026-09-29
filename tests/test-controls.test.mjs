import assert from "node:assert/strict";
import test from "node:test";
import { readProbeResponse, readTestEvents } from "../static/test-controls.js";

function streamed(packets) {
  const bytes = new TextEncoder().encode(packets.map((packet) => JSON.stringify(packet) + "\n").join(""));
  return new Response(new ReadableStream({
    start(controller) {
      // 刻意把 UTF-8 字符和 JSON 帧切在不同的网络块中。
      for (let i = 0; i < bytes.length; i += 2) controller.enqueue(bytes.slice(i, i + 2));
      controller.close();
    },
  }), { headers: { "Content-Type": "application/x-ndjson" } });
}

test("流式跨块解码，只在收到最终结果后完成", async () => {
  const parts = [];
  const result = await readProbeResponse(streamed([
    { type: "start" }, { type: "delta", text: "中文 1," }, { type: "delta", text: " 2" },
    { type: "result", text: "中文 1, 2", accepted: true },
  ]), true, (text) => parts.push(text));
  assert.deepEqual(parts, ["中文 1,", " 2"]);
  assert.equal(result.accepted, true);
});

test("断流和流内错误拒绝半截回答", async () => {
  await assert.rejects(readProbeResponse(streamed([{ type: "delta", text: "1,2" }]), true, () => {}), /连接中断/);
  await assert.rejects(readProbeResponse(streamed([{ type: "error", error: "上游失败" }]), true, () => {}), /上游失败/);
});

test("关闭流式使用 JSON，HTTP 错误保留具体原因", async () => {
  assert.deepEqual(await readProbeResponse(Response.json({ accepted: true }), false, () => {}), { accepted: true });
  await assert.rejects(readProbeResponse(Response.json({ error: "参数错误" }, { status: 400 }), true, () => {}), /参数错误/);
});

test("测试工作区接收挑战进度和嵌套结果，挑战失败不提前终止整轮", async () => {
  const events = [];
  const result = await readTestEvents(streamed([
    { type: "start", run_id: "example" },
    { type: "challenge", attempt: 1 },
    { type: "challenge_error", error: "本次挑战失败" },
    { type: "challenge", attempt: 2 },
    { type: "challenge_result", accepted: true },
    { type: "result", run_id: "example", result: { used_outputs: 1 } },
  ]), (event) => events.push(event));
  assert.equal(events.length, 6);
  assert.equal(result.result.used_outputs, 1);
  assert.equal(result.run_id, "example");
});
