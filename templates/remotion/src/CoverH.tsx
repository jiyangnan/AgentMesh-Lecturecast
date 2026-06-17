import { AbsoluteFill } from 'remotion';
import { COLORS as C, FONTS as F, BRAND } from './theme';

// 横版封面 1920×1080 (B站/YouTube)。每期改标题/标语；底部禁用导流话术（见 SKILL.md 合规节）。
export const CoverH: React.FC = () => {
  return (
    <AbsoluteFill style={{ background: C.bg, fontFamily: F.sans, color: C.ink, padding: '80px 100px', justifyContent: 'space-between' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, fontSize: 30, fontFamily: F.mono, letterSpacing: '0.2em', fontWeight: 800 }}>
          <div style={{ width: 18, height: 18, background: C.accent }} />
          <span style={{ color: C.accent }}>{BRAND.series} · {BRAND.ep}</span>
        </div>
        <div style={{ fontFamily: F.mono, fontSize: 24, color: C.accent, border: `3px solid ${C.accent}`, borderRadius: 8, padding: '8px 18px', fontWeight: 800, letterSpacing: '0.15em' }}>手把手</div>
      </div>
      {/* 👇 每期改标题 */}
      <div>
        <div style={{ fontSize: 56, fontWeight: 800, color: C.gray, marginBottom: 14 }}>给一个链接</div>
        <div style={{ fontSize: 150, fontWeight: 900, letterSpacing: -3, lineHeight: 1 }}>
          <span style={{ color: C.accent }}>10 分钟</span> 拆解<span style={{ background: C.yellow, padding: '0 16px' }}>任何爆款博主</span>
        </div>
        <div style={{ fontSize: 48, fontWeight: 700, color: C.ink2, marginTop: 26 }}>采集 → 分析 → 看板 → 开源 · 全链手把手</div>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 26, fontFamily: F.mono, letterSpacing: '0.12em', fontWeight: 700, color: C.gray }}>
        <span>白羊武士</span>
        <span style={{ color: C.accent }}>把爆款 · 逆向成方法</span>
      </div>
    </AbsoluteFill>
  );
};
