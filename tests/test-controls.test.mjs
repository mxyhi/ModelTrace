import assert from "node:assert/strict";
import test from "node:test";
import { readTestEvents } from "../static/test-controls.js";

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

test("跨块解码，只在收到最终结果后完成", async () => {
  const deltas = [];
  const result = await readTestEvents(streamed([
    { type: "start" }, { type: "delta", text: "中文 1," }, { type: "delta", text: " 2" },
    { type: "result", result: { used_outputs: 3 } },
  ]), (event) => { if (event.type === "delta") deltas.push(event.text); });
  assert.deepEqual(deltas, ["中文 1,", " 2"]);
  assert.equal(result.result.used_outputs, 3);
});

test("断流、流内错误和 HTTP 错误都拒绝半截结果", async () => {
  await assert.rejects(readTestEvents(streamed([{ type: "delta", text: "1,2" }]), () => {}), /连接中断/);
  await assert.rejects(readTestEvents(streamed([{ type: "error", error: "上游失败" }]), () => {}), /上游失败/);
  await assert.rejects(readTestEvents(Response.json({ error: "参数错误" }, { status: 400 }), () => {}), /参数错误/);
  await assert.rejects(readTestEvents(Response.json({ accepted: true }), () => {}), /未返回预期的进度数据/);
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
