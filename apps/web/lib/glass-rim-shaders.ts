/**
 * 液态玻璃「边缘透镜」着色器（components/glass-tab-bar.tsx 的 WebGL 边缘层）。
 *
 * 光学模型移植自 iyinchao/liquid-glass-studio（MIT，Copyright (c) 2024 Charles Yin，
 * src/shaders/fragment-main.glsl 与 lib/color.glsl）：
 *   - 折射：把玻璃边缘看成一段圆弧厚边，入射角 θi = asin((1 - 距边距离/厚度)²)，
 *     按折射率求折射角 θt，偏移量 = tan(θi - θt)——越靠外沿偏移越大，把内侧内容
 *     「拉」到边上，形成透镜挤压感；
 *   - 色散：R / G / B 三个通道按各自折射率（1∓0.02×色散系数）取不同偏移量；
 *   - 菲涅尔：贴着外沿 2~3px 的一圈在 LCH 空间提亮度；
 *   - 高光（glare）：按法线方向与光源角的夹角在外沿打亮边，对侧按比例弱一些。
 * 与 studio 的差别：
 *   - 形状是 1~3 个圆角矩形（主胶囊 / 搜索圆钮 / 底部附件），坐标一律用视口 CSS 像素，
 *     圆角是标准圆弧（与 CSS border-radius 的裁切完全重合，超椭圆会被裁掉一角）；
 *   - 只画「厚边」这一圈：进入胶囊中心（距边 ≥ 厚度）后透明度渐变到 0，中心
 *     交给 CSS 层透出真实的滚动内容（着色器采样的场景里只有图片、
 *     没有文字，中心若也由它画，滚过底栏的文字就消失了）；
 *   - 厚边默认全程取模糊画面（studio 的 blurEdge=true 口径），与 CSS 中心同一质地；
 *     也可切到外沿锐利、向内渐糊（blurEdge=false）。
 */

export const VERTEX_SHADER = `#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
  v_uv = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
`;

/** 可分离高斯模糊（横 / 竖各一遍），sigma 以纹素计，权重在着色器里现算 */
export const BLUR_SHADER = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_input;
uniform vec2 u_texel;
uniform vec2 u_dir;
uniform float u_sigma;
out vec4 fragColor;
void main() {
  int radius = int(ceil(u_sigma * 3.0));
  vec4 sum = texture(u_input, v_uv);
  float total = 1.0;
  for (int i = 1; i <= 48; i++) {
    if (i > radius) break;
    float w = exp(-float(i * i) / (2.0 * u_sigma * u_sigma));
    vec2 o = u_dir * u_texel * float(i);
    sum += (texture(u_input, v_uv + o) + texture(u_input, v_uv - o)) * w;
    total += 2.0 * w;
  }
  fragColor = sum / total;
}
`;

export const MAIN_SHADER = `#version 300 es
precision highp float;

#define PI 3.14159265359
const float N_R = 1.0 - 0.02;
const float N_G = 1.0;
const float N_B = 1.0 + 0.02;

uniform sampler2D u_scene;
uniform sampler2D u_blurred;
uniform vec2 u_res;
uniform float u_scale;
uniform vec2 u_origin;
uniform vec2 u_size;
uniform int u_count;
uniform vec4 u_shapes[3];
uniform float u_radius[3];

uniform float u_thickness;
uniform float u_refPx;
uniform float u_refFactor;
uniform float u_dispersion;
uniform float u_fresnelRange;
uniform float u_fresnelHardness;
uniform float u_fresnelFactor;
uniform float u_glareRange;
uniform float u_glareHardness;
uniform float u_glareConvergence;
uniform float u_glareOpposite;
uniform float u_glareFactor;
uniform float u_glareAngle;
uniform vec4 u_tint;
uniform float u_saturate;
uniform float u_fadeStart;
// 1 = 厚边全程取模糊画面（studio 默认 blurEdge）；0 = 外沿锐利、向内渐糊
uniform float u_blurEdge;

out vec4 fragColor;

float safeAsin(float x) { return asin(clamp(x, -1.0, 1.0)); }

float sdRoundBox(vec2 p, vec2 halfSize, float r) {
  vec2 q = abs(p) - halfSize + r;
  return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

float sceneSDF(vec2 p) {
  float d = 1e5;
  for (int i = 0; i < 3; i++) {
    if (i >= u_count) break;
    vec4 s = u_shapes[i];
    d = min(d, sdRoundBox(p - s.xy, s.zw, u_radius[i]));
  }
  return d;
}

// —— LCH 色彩空间（studio lib/color.glsl，D65 白点）——
const vec3 WHITE = vec3(0.95045592705, 1.0, 1.08905775076);
const mat3 RGB_TO_XYZ_M = mat3(0.4124, 0.3576, 0.1805, 0.2126, 0.7152, 0.0722, 0.0193, 0.1192, 0.9505);
const mat3 XYZ_TO_RGB_M = mat3(3.2406255, -1.537208, -0.4986286, -0.9689307, 1.8757561, 0.0415175, 0.0557101, -0.2040211, 1.0569959);
float uncompand(float a) { return a > 0.04045 ? pow((a + 0.055) / 1.055, 2.4) : a / 12.92; }
float compand(float a) { return a <= 0.0031308 ? 12.92 * a : 1.055 * pow(a, 0.41666666666) - 0.055; }
float labF(float x) { return x > 0.00885645167 ? pow(x, 0.333333333) : 7.78703703704 * x + 0.13793103448; }
float labFInv(float x) { return x > 0.206897 ? x * x * x : 0.12841854934 * (x - 0.137931034); }
vec3 srgbToLch(vec3 c) {
  vec3 xyz = vec3(uncompand(c.r), uncompand(c.g), uncompand(c.b)) * RGB_TO_XYZ_M / WHITE;
  xyz = vec3(labF(xyz.x), labF(xyz.y), labF(xyz.z));
  vec3 lab = vec3(116.0 * xyz.y - 16.0, 500.0 * (xyz.x - xyz.y), 200.0 * (xyz.y - xyz.z));
  return vec3(lab.x, length(lab.yz), atan(lab.z, lab.y) * 57.2957795131);
}
vec3 lchToSrgb(vec3 lch) {
  vec3 lab = vec3(lch.x, lch.y * cos(lch.z * 0.01745329251), lch.y * sin(lch.z * 0.01745329251));
  float w = (lab.x + 16.0) / 116.0;
  vec3 xyz = WHITE * vec3(labFInv(w + lab.y / 500.0), labFInv(w), labFInv(w - lab.z / 200.0));
  vec3 rgb = xyz * XYZ_TO_RGB_M;
  return clamp(vec3(compand(rgb.r), compand(rgb.g), compand(rgb.b)), 0.0, 1.0);
}

vec3 saturateColor(vec3 c, float s) {
  float l = dot(c, vec3(0.2126, 0.7152, 0.0722));
  return clamp(mix(vec3(l), c, s), 0.0, 1.0);
}

vec3 sampleDispersed(vec2 uv, vec2 offsetUv, float blurMix) {
  float fr = 1.0 - (N_R - 1.0) * u_dispersion;
  float fg = 1.0 - (N_G - 1.0) * u_dispersion;
  float fb = 1.0 - (N_B - 1.0) * u_dispersion;
  vec3 sharp = vec3(
    texture(u_scene, uv + offsetUv * fr).r,
    texture(u_scene, uv + offsetUv * fg).g,
    texture(u_scene, uv + offsetUv * fb).b
  );
  vec3 blurred = vec3(
    texture(u_blurred, uv + offsetUv * fr).r,
    texture(u_blurred, uv + offsetUv * fg).g,
    texture(u_blurred, uv + offsetUv * fb).b
  );
  return mix(sharp, blurred, blurMix);
}

void main() {
  // 片元 → 视口 CSS 坐标（y 向下）
  vec2 p = vec2(gl_FragCoord.x, u_res.y - gl_FragCoord.y) / u_scale + u_origin;
  float d = sceneSDF(p);
  if (d > 1.0) {
    fragColor = vec4(0.0);
    return;
  }
  float dist = max(-d, 0.0);
  if (dist >= u_thickness) {
    fragColor = vec4(0.0);
    return;
  }

  // 外法线（中心差分），css 坐标系
  float e = 0.5;
  vec2 n = vec2(sceneSDF(p + vec2(e, 0.0)) - sceneSDF(p - vec2(e, 0.0)),
                sceneSDF(p + vec2(0.0, e)) - sceneSDF(p - vec2(0.0, e)));
  n = length(n) > 1e-5 ? normalize(n) : vec2(0.0, 1.0);

  // 折射：厚边圆弧模型
  float edgeH = dist / u_thickness;
  float xr = 1.0 - edgeH;
  float thetaI = safeAsin(xr * xr);
  float thetaT = safeAsin(sin(thetaI) / u_refFactor);
  float edgeFactor = -tan(thetaT - thetaI);

  vec2 uv = (p - u_origin) / u_size;
  vec2 offsetUv = (-n * edgeFactor * u_refPx) / u_size;
  vec3 color = sampleDispersed(uv, offsetUv, u_blurEdge > 0.5 ? 1.0 : edgeH);
  color = saturateColor(color, u_saturate);
  vec3 base = color;
  float tintA = u_tint.a;
  color = mix(color, u_tint.rgb, tintA);

  // 菲涅尔：外沿一圈提亮（d 为 CSS 像素，内部为负；底数先截到 ≥0，负底数的 pow 在 GLSL 里无定义）
  float fresnel = clamp(pow(max(1.0 + d / 1500.0 * pow(500.0 / u_fresnelRange, 2.0) + u_fresnelHardness, 0.0), 5.0), 0.0, 1.0);
  vec3 fresnelLch = srgbToLch(mix(vec3(1.0), u_tint.rgb, tintA * 0.5));
  fresnelLch.x = clamp(fresnelLch.x + 20.0 * fresnel * u_fresnelFactor, 0.0, 100.0);
  color = mix(color, lchToSrgb(fresnelLch), fresnel * u_fresnelFactor * 0.7);

  // 高光：studio 的角度模型；法线换回 y 向上再求角，与 studio 的光源角口径一致
  float glareGeo = clamp(pow(max(1.0 + d / 1500.0 * pow(500.0 / u_glareRange, 2.0) + u_glareHardness, 0.0), 5.0), 0.0, 1.0);
  float ang = atan(-n.y, n.x);
  if (ang < 0.0) ang += 2.0 * PI;
  float glareAngle = (ang - PI / 4.0 + u_glareAngle) * 2.0;
  bool farside = (glareAngle > PI * 1.5 && glareAngle < PI * 3.5) || glareAngle < PI * -0.5;
  float glareFactor = (0.5 + sin(glareAngle) * 0.5) * (farside ? 1.2 * u_glareOpposite : 1.2) * u_glareFactor;
  glareFactor = clamp(pow(glareFactor, 0.1 + u_glareConvergence * 2.0), 0.0, 1.0);
  vec3 glareLch = srgbToLch(mix(base, u_tint.rgb, tintA * 0.5));
  glareLch.x = clamp(glareLch.x + 150.0 * glareFactor * glareGeo, 0.0, 120.0);
  glareLch.y += 30.0 * glareFactor * glareGeo;
  color = mix(color, lchToSrgb(glareLch), glareFactor * glareGeo);

  // 内缘渐隐交给 CSS 中心；外沿 1px 抗锯齿
  float alpha = (1.0 - smoothstep(u_fadeStart, 1.0, edgeH)) * (1.0 - smoothstep(-0.5, 0.5, d));
  fragColor = vec4(color * alpha, alpha);
}
`;
