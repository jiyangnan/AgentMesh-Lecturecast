import { Composition } from 'remotion';
import { Video } from './Video';
import { VideoH } from './VideoH';
import { Cover } from './Cover';
import { CoverH } from './CoverH';
import { TOTAL_FRAMES, FPS, W, H } from './theme';

// 4 compositions from ONE project:
//   VideoVertical  1080×1920  → 小红书 / 抖音 / Reels
//   VideoLandscape 1920×1080  → B站 / YouTube
//   CoverVertical  1242×1660  → 小红书封面
//   CoverLandscape 1920×1080  → B站封面
export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition id="VideoVertical" component={Video} durationInFrames={TOTAL_FRAMES} fps={FPS} width={W} height={H} />
      <Composition id="VideoLandscape" component={VideoH} durationInFrames={TOTAL_FRAMES} fps={FPS} width={1920} height={1080} />
      <Composition id="CoverVertical" component={Cover} durationInFrames={30} fps={FPS} width={1242} height={1660} />
      <Composition id="CoverLandscape" component={CoverH} durationInFrames={30} fps={FPS} width={1920} height={1080} />
    </>
  );
};
