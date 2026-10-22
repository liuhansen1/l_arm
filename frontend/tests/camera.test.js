import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { formatCoordinateInput, pointOnGround, projectPosition, roundedPosition } from "../coordinates.js";
import { readFrameStream } from "../frame_stream.js";

const scene = JSON.parse(await readFile(new URL("../assets/scene.json", import.meta.url), "utf8"));
const image = new Uint8Array(await readFile(new URL("../assets/scene.png", import.meta.url)));

function packet(id) {
  const metadata = new TextEncoder().encode(JSON.stringify({ frame_id: id, image_size: image.length }));
  const data = new Uint8Array(4 + metadata.length + image.length);
  new DataView(data.buffer).setUint32(0, metadata.length);
  data.set(metadata, 4);
  data.set(image, 4 + metadata.length);
  return data;
}

function stream(chunks) {
  return new ReadableStream({ start(controller) { chunks.forEach(chunk => controller.enqueue(chunk)); controller.close(); } });
}

test("坐标显示和记录统一为三位小数", () => {
  assert.deepEqual(roundedPosition([0.551234, -0.123678, 0.025123]), [0.551, -0.124, 0.025]);
  const input = { value: "0.55", valueAsNumber: 0.55 };
  formatCoordinateInput(input);
  assert.equal(input.value, "0.550");
  assert.throws(() => roundedPosition([NaN, 0, 0]));
});

test("任意地面点可投影并反投影，保留毫米记录精度", () => {
  const point = [0.55, -0.08, 0];
  const pixel = projectPosition(point, scene.camera);
  assert.ok(pixel);
  assert.deepEqual(pointOnGround(...pixel, scene.camera), point);
});

test("相机流允许帧头、JSON 和 PNG 被分成任意网络块", async () => {
  const bytes = packet("frame-1");
  const chunks = [bytes.slice(0, 1), bytes.slice(1, 3), bytes.slice(3, 11), bytes.slice(11, 60), bytes.slice(60)];
  const frames = [];
  for await (const frame of readFrameStream(stream(chunks))) frames.push(frame);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].metadata.frame_id, "frame-1");
  assert.deepEqual(new Uint8Array(await frames[0].image.arrayBuffer()), image);
});

test("一个网络块可同时包含多帧，图片与帧 ID 配对", async () => {
  const first = packet("frame-1"), second = packet("frame-2");
  const bytes = new Uint8Array(first.length + second.length);
  bytes.set(first); bytes.set(second, first.length);
  const ids = [];
  for await (const frame of readFrameStream(stream([bytes]))) ids.push(frame.metadata.frame_id);
  assert.deepEqual(ids, ["frame-1", "frame-2"]);
});

test("残缺或不合法相机帧会报告错误", async () => {
  async function consume(chunks) { for await (const frame of readFrameStream(stream(chunks))) void frame; }
  await assert.rejects(consume([packet("frame-1").slice(0, 20)]), /不完整/);
  await assert.rejects(consume([new Uint8Array([255, 255, 255, 255])]), /帧头/);
});
