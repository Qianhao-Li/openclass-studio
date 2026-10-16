import type { LessonPackageControlsProps } from "../components/course-studio/lesson-package-controls";
import type { useLessonPackage } from "../hooks/course-studio/use-lesson-package";

export function lessonPackageControlProps(lessonPackage: ReturnType<typeof useLessonPackage>): LessonPackageControlsProps {
  return {
    currentStep: lessonPackage.currentStep, stepIndex: lessonPackage.stepIndex, stepCount: lessonPackage.steps.length,
    isPlaying: lessonPackage.isPlaying, isPlaybackActive: lessonPackage.isPlaybackActive,
    speed: lessonPackage.speed, operation: lessonPackage.operation, onSpeedChange: lessonPackage.setSpeed,
    onPlayToggle: lessonPackage.startOrTogglePlayback, onPrevious: () => lessonPackage.movePlayback(-1),
    onNext: () => lessonPackage.movePlayback(1), onExit: lessonPackage.exitPlayback,
    onFork: lessonPackage.forkFromCurrentStep, onExport: lessonPackage.exportRidoc,
    onImport: async (file) => { await lessonPackage.importRidoc(file); },
  };
}
