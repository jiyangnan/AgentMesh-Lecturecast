import { AbsoluteFill } from 'remotion';
import { COLORS, FONTS, BRAND } from './theme';

// 竖版封面 1242×1660 (小红书)。每期改标题/标语；底部禁用导流话术（见 SKILL.md 合规节）。
export const Cover: React.FC = () => {
  return (
    <AbsoluteFill style={{ background: COLORS.bg, fontFamily: FONTS.sans, color: COLORS.ink, padding: '90px 70px', justifyContent: 'space-between' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, fontSize: 28, fontFamily: FONTS.mono, letterSpacing: '0.22em', fontWeight: 800 }}>
          <div style={{ width: 16, height: 16, background: COLORS.accent }} />
          <span style={{ color: COLORS.accent }}>{BRAND.series} · {BRAND.ep}</span>
        </div>
        <div style={{ fontFamily: FONTS.mono, fontSize: 20, color: COLORS.accent, border: `3px solid ${COLORS.accent}`, borderRadius: 8, padding: '6px 14px', fontWeight: 800, letterSpacing: '0.15em' }}>
          手把手
        </div>
      </div>

      {/* 👇 每期改标题 */}
      <div>
        <div style={{ fontSize: 50, fontWeight: 800, color: COLORS.gray, marginBottom: 10, lineHeight: 1.0 }}>
          给一个链接
        </div>
        <div style={{ fontSize: 150, fontWeight: 900, color: COLORS.accent, letterSpacing: '-0.05em', lineHeight: 0.95, marginBottom: 24 }}>
          10 分钟
        </div>
        <div style={{ fontSize: 64, fontWeight: 900, color: COLORS.ink, lineHeight: 1.25 }}>
          拆解<span style={{ background: COLORS.yellow, padding: '4px 14px' }}>任何爆款博主</span><br />
          的全套方法论
        </div>
      </div>

      <div>
        <div style={{ display: 'flex', gap: 14, marginBottom: 40 }}>
          {['采集', '分析', '看板', '开源'].map((t, i) => (
            <div key={i} style={{ flex: 1, padding: '14px 8px', background: COLORS.paper, border: `3px solid ${COLORS.accent}`, textAlign: 'center', fontFamily: FONTS.mono, fontSize: 20, fontWeight: 800, color: COLORS.accent, borderRadius: 8 }}>{t}</div>
          ))}
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 22, fontFamily: FONTS.mono, letterSpacing: '0.12em', fontWeight: 700, color: COLORS.gray }}>
          <span>白羊武士</span>
          <span style={{ color: COLORS.accent }}>把爆款 · 逆向成方法</span>
        </div>
      </div>
    </AbsoluteFill>
  );
};
