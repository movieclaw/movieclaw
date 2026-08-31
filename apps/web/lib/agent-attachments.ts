/**
 * Agent 会话的图片附件：上传前压缩 + 上传 + 本地预览。
 *
 * 压缩在前端做（canvas 最长边 2048、JPEG 0.85）：视觉模型的有效分辨率就在
 * 这个量级，供应商还会再自行缩放；压缩后手机原图从几 MB 降到几百 KB，服务端
 * 只兜底校验（≤5MB、魔数嗅探）。GIF 不压缩（canvas 会丢动画帧），原样上传。
 */

import { type AgentAttachmentUpload, uploadSessionAttachment } from "@/lib/api/agent";

/** composer 里的一张待发送图片（已上传，等待随消息引用）。 */
export interface ComposerImage {
  attachmentId: string;
  name: string;
  /** 本地 objectURL：chip 与乐观气泡直接渲染，不回读服务端 */
  previewUrl: string;
}

/** 每条消息的图片上限（与服务端 MAX_ATTACHMENTS_PER_MESSAGE 一致）。 */
export const MAX_IMAGES_PER_MESSAGE = 4;

const MAX_EDGE = 2048;
const JPEG_QUALITY = 0.85;
const ACCEPTED_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);

export function isAcceptedImage(file: File): boolean {
  return ACCEPTED_TYPES.has(file.type);
}

/** 超过 2048 边长时用 canvas 缩到 JPEG；小图与 GIF 原样返回。 */
async function compressImage(file: File): Promise<{ blob: Blob; name: string }> {
  if (file.type === "image/gif") return { blob: file, name: file.name };
  const bitmap = await createImageBitmap(file).catch(() => null);
  if (!bitmap) return { blob: file, name: file.name };
  try {
    const scale = MAX_EDGE / Math.max(bitmap.width, bitmap.height);
    if (scale >= 1) return { blob: file, name: file.name };
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    const context = canvas.getContext("2d");
    if (!context) return { blob: file, name: file.name };
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY),
    );
    if (!blob) return { blob: file, name: file.name };
    const name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
    return { blob, name: name === ".jpg" ? "图片.jpg" : name };
  } finally {
    bitmap.close();
  }
}

/** 压缩并上传一张图片，返回可入 composer 的附件对象；失败时抛出中文错误。 */
export async function prepareImageAttachment(file: File): Promise<ComposerImage> {
  if (!isAcceptedImage(file)) {
    throw new Error("不支持的图片格式，请选择 JPG / PNG / WebP / GIF 图片");
  }
  const { blob, name } = await compressImage(file);
  const uploaded: AgentAttachmentUpload = await uploadSessionAttachment(blob, name);
  return {
    attachmentId: uploaded.attachment_id,
    name: uploaded.name,
    // objectURL 生命周期与页面同长（发送后的乐观气泡还要用它渲染），
    // 单会话最多百来张小图，不做显式 revoke
    previewUrl: URL.createObjectURL(blob),
  };
}
