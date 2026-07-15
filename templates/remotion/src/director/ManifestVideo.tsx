import React, {useMemo} from 'react';
import {AbsoluteFill, Audio, Sequence, staticFile} from 'remotion';
import {ManifestProvider} from './ManifestContext';
import {palette} from './layout';
import {componentRegistry} from './registry';
import {DirectorRenderProps, ManifestScene} from './types';
import {validateManifestForRender} from './validate';

const withOverrides = (scene: ManifestScene, overrides: DirectorRenderProps['overrides']): ManifestScene => {
  const timing = overrides.scene_timing?.[scene.scene_id] ?? {};
  return {
    ...scene,
    start_frame: timing.start_frame ?? scene.start_frame,
    duration_frames: timing.duration_frames ?? scene.duration_frames,
    props: {...scene.props, ...(overrides.scene_props?.[scene.scene_id] ?? {})},
  };
};

export const ManifestVideo: React.FC<DirectorRenderProps> = (renderProps) => {
  const {manifest, overrides, variant, audioSrc} = renderProps;
  validateManifestForRender(manifest);
  const scenes = useMemo(() => manifest.scenes.map((scene) => withOverrides(scene, overrides)), [manifest, overrides]);
  return (
    <ManifestProvider {...renderProps}>
      <AbsoluteFill style={{background: palette.background}}>
        {audioSrc ? <Audio src={staticFile(audioSrc)} /> : null}
        {scenes.map((scene) => {
          const Component = componentRegistry[scene.component_id];
          return (
            <Sequence key={scene.scene_id} from={scene.start_frame} durationInFrames={scene.duration_frames} name={scene.scene_id}>
              <Component scene={scene} props={scene.props} variant={variant} />
            </Sequence>
          );
        })}
      </AbsoluteFill>
    </ManifestProvider>
  );
};

