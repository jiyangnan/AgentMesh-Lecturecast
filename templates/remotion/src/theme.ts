// Lecturecast · Remotion visual system
// ── Per-SERIES: edit BRAND + COLORS.accent (see docs/CUSTOMIZATION.md for presets)
// ── Per-EPISODE: SECTIONS is auto-filled by update_theme.py from audio/*.json

export const BRAND = {
  series: 'AI 实战教程',   // 系列名，显示在每个场景左上角的品牌条
  ep: 'EP1',               // 本期编号
};

export const COLORS = {
  bg: '#FFF7F0',          // cream background (keep warm; never full-color except EndCard)
  ink: '#1A1A1A',
  gray: '#8E8E8E',
  paper: '#FFFFFF',
  accent: '#FF5C00',      // 系列主色 · 赤焰橙=实战线 / 蓝橙=MCP / 紫=RAG / 翡翠绿=Agent
  accentDark: '#D94A00',
  accentLight: '#FFE7D6',
  ink2: '#3A3A3A',
  green: '#27D796',
  greenDark: '#0E8A6E',
  blue: '#2E5BFF',
  yellow: '#FFD400',
  red: '#FF2442',
  codeBg: '#1B1B27',
  codeFg: '#D6D6E0',
  codeGreen: '#7EE787',
  codeOrange: '#FAB387',
  codePink: '#F38BA8',
  codeBlue: '#89B4FA',
  codeYellow: '#FEBC2E',
  codeRed: '#FF7B72',
  codeGray: '#7A7A92',
  codeCyan: '#56D4DD',
};

export const FONTS = {
  sans: '"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans SC",sans-serif',
  mono: '"SF Mono","JetBrains Mono",Menlo,monospace',
};

export const FPS = 30;
export const W = 1080;   // 竖版 (小红书/抖音/Reels) — 横版 1920×1080 在 Root.tsx 直接声明
export const H = 1920;

// ── Per-episode timing — DO NOT hand-edit; run `python3 update_theme.py` after配音 ──
// (示例值来自 teardown EP1 的 hook + end 两段；真实项目会有 7-8 段)
export const SECTIONS: { id: string; start: number; duration: number }[] = [
  { id: 'hook', start: 0, duration: 26.25 },
  { id: 'end', start: 26.25, duration: 20.77 },
];

export const NARRATION_SEC = 47.02;
export const TOTAL_SEC = 47.02;
export const TOTAL_FRAMES = Math.ceil(TOTAL_SEC * FPS);
