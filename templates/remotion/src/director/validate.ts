import catalog from './component-catalog.json';
import {ManifestScene, ProductionManifest, RenderTiming} from './types';

export const validateManifestForRender = (manifest: ProductionManifest): void => {
  if (manifest.schema_version !== '1.0') throw new Error('unsupported manifest schema_version');
  if (!Number.isInteger(manifest.total_frames) || manifest.total_frames <= 0) throw new Error('invalid total_frames');
  const componentIds = new Set(catalog.components.map((component) => component.component_id));
  const sceneIds = new Set<string>();
  for (const scene of manifest.scenes) {
    if (sceneIds.has(scene.scene_id)) throw new Error(`duplicate scene_id ${scene.scene_id}`);
    sceneIds.add(scene.scene_id);
    if (!componentIds.has(scene.component_id)) throw new Error(`unknown component ${scene.component_id}`);
    if (scene.start_frame < 0 || scene.duration_frames <= 0 || scene.start_frame + scene.duration_frames > manifest.total_frames) {
      throw new Error(`scene timing exceeds manifest: ${scene.scene_id}`);
    }
  }
};

export const validateRenderTiming = (
  timing: RenderTiming | undefined,
  manifest: ProductionManifest,
): void => {
  if (!timing) return;
  if (!Number.isInteger(timing.total_frames) || timing.total_frames <= 0) {
    throw new Error('invalid render timing total_frames');
  }
  const sceneIds = new Set(manifest.scenes.map((scene) => scene.scene_id));
  const timingSceneIds = Object.keys(timing.scene_timing);
  if (timingSceneIds.length !== sceneIds.size || timingSceneIds.some((sceneId) => !sceneIds.has(sceneId))) {
    throw new Error('render timing must cover every manifest scene exactly once');
  }
  for (const [sceneId, value] of Object.entries(timing.scene_timing)) {
    if (!sceneIds.has(sceneId)) throw new Error(`render timing references unknown scene ${sceneId}`);
    if (
      !Number.isInteger(value.start_frame) ||
      !Number.isInteger(value.duration_frames) ||
      value.start_frame < 0 ||
      value.duration_frames <= 0 ||
      value.start_frame + value.duration_frames > timing.total_frames
    ) {
      throw new Error(`render timing exceeds execution timeline: ${sceneId}`);
    }
  }
};

export const validateSceneExecution = (scenes: ManifestScene[], totalFrames: number): void => {
  for (const scene of scenes) {
    if (
      !Number.isInteger(scene.start_frame) ||
      !Number.isInteger(scene.duration_frames) ||
      scene.start_frame < 0 ||
      scene.duration_frames <= 0 ||
      scene.start_frame + scene.duration_frames > totalFrames
    ) {
      throw new Error(`effective scene timing exceeds execution timeline: ${scene.scene_id}`);
    }
  }
};
