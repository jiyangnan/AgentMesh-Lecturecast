import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { COLORS as C, FONTS as F, BRAND } from '../theme';
import { reveal, pop, pulse } from '../anim';

export const End: React.FC = () => {
  const frame = useCurrentFrame();
  const polls = ['AI 自动剪视频', 'AI 做小红书图文', 'AI 做数据采集', '你来定 →'];
  return (
    <AbsoluteFill style={{ background: C.ink, fontFamily: F.sans, color: C.paper }}>
      <div style={{ position: 'absolute', top: 130, left: 0, right: 0, textAlign: 'center', ...reveal(frame, 0.1) }}>
        <span style={{ fontSize: 34, fontWeight: 800, color: C.accent, letterSpacing: 2 }}>{BRAND.series} · {BRAND.ep}</span>
      </div>

      {/* CTA card — 纯软钩子，零导流零诱导关注（2026-06-17 修正，防限流） */}
      <div style={{ position: 'absolute', top: 360, left: 70, right: 70, ...pop(frame, 0.8) }}>
        <div style={{ background: C.accent, borderRadius: 28, padding: '60px 50px', textAlign: 'center', boxShadow: '0 20px 60px rgba(255,92,0,.35)' }}>
          <div style={{ fontSize: 46, fontWeight: 700, opacity: .95 }}>把一个爆款</div>
          <div style={{ fontSize: 84, fontWeight: 900, lineHeight: 1.2, marginTop: 18, ...pulse(frame, 2) }}>
            逆向成「<span style={{ color: C.ink }}>方法</span>」
          </div>
          <div style={{ fontSize: 44, fontWeight: 700, marginTop: 22 }}>这个视角，谁都能用</div>
        </div>
        <div style={{ fontSize: 30, color: '#bbb', textAlign: 'center', marginTop: 22, ...reveal(frame, 3) }}>
          工具已开源 · 学方法，不抄搬运
        </div>
      </div>

      {/* next poll */}
      <div style={{ position: 'absolute', top: 980, left: 70, right: 70, ...reveal(frame, 5, { dy: 24 }) }}>
        <div style={{ fontSize: 40, fontWeight: 800, marginBottom: 24, textAlign: 'center' }}>下期实战，你想看哪个？</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18, justifyContent: 'center' }}>
          {polls.map((p, i) => (
            <span key={i} style={{ fontSize: 34, fontWeight: 700, border: `2px solid ${C.accent}`, color: C.paper, borderRadius: 14, padding: '16px 30px', ...pop(frame, 5.5 + i * 0.4) }}>{p}</span>
          ))}
        </div>
      </div>
    </AbsoluteFill>
  );
};
