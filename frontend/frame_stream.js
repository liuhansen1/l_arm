/** 流协议：uint32 大端 JSON 长度 + JSON（含 image_size）+ PNG 字节。 */
export async function* readFrameStream(stream) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = new Uint8Array();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        if (buffer.length) throw new Error("相机数据流中断，收到不完整的帧。");
        return;
      }
      const joined = new Uint8Array(buffer.length + value.length);
      joined.set(buffer);
      joined.set(value, buffer.length);
      buffer = joined;
      while (buffer.length >= 4) {
        const length = new DataView(buffer.buffer, buffer.byteOffset, 4).getUint32(0);
        if (!length || length > 65536) throw new Error("相机帧头不合法。");
        if (buffer.length < 4 + length) break;
        const metadata = JSON.parse(decoder.decode(buffer.subarray(4, 4 + length)));
        const size = metadata.image_size;
        if (!Number.isInteger(size) || size <= 0 || size > 8 * 1024 * 1024) throw new Error("相机图像大小不合法。");
        const end = 4 + length + size;
        if (buffer.length < end) break;
        yield { metadata, image: new Blob([buffer.slice(4 + length, end)], { type: "image/png" }) };
        buffer = buffer.slice(end);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
