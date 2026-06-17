import { AbsoluteFill, Audio, Sequence, staticFile } from 'remotion';
import { COLORS, FPS, FONTS, SECTIONS } from './theme';
import { HookH } from './scenesH/HookH';
import { EndH } from './scenesH/EndH';
// 👉 横版 (B站/YouTube)。每期给每个 section 写一个 scenesH/<Id>H.tsx。
//    用 kitH 的 StageH/TitleH/ShotH 原语；底部只留 ~150px 给字幕（竖版留 ~340px）。

const SCENES: Record<string, React.FC> = {
  hook: HookH,
  end: EndH,
  // why: WhyH, collect: CollectH, ... ← 每期补齐
};

export const VideoH: React.FC = () => {
  return (
    <AbsoluteFill style={{ background: COLORS.bg, fontFamily: FONTS.sans, color: COLORS.ink }}>
      <Audio src={staticFile('narration.mp3')} />
      {SECTIONS.map(({ id, start, duration }) => {
        const Comp = SCENES[id];
        if (!Comp) return null;
        return (
          <Sequence key={id} from={Math.round(start * FPS)} durationInFrames={Math.round(duration * FPS)} name={id}>
            <Comp />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
