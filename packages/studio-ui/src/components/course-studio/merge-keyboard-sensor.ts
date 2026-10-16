import { KeyboardSensor, type Activators, type KeyboardSensorOptions, type SensorProps } from "@dnd-kit/core";

type MergeKeyboardOptions = KeyboardSensorOptions & {
  onStep: (direction: "up" | "down") => void;
};

/** Keyboard movement changes a whole group, independently of pointer geometry. */
export class MergeKeyboardSensor {
  static activators: Activators<MergeKeyboardOptions> = KeyboardSensor.activators;
  autoScrollEnabled = false;
  private readonly document: Document;
  private readonly handle: HTMLElement | null;

  constructor(private readonly props: SensorProps<MergeKeyboardOptions>) {
    this.handle = props.activeNode.activatorNode.current ?? props.activeNode.node.current;
    this.document = this.handle?.ownerDocument ?? document;
    // Capture keeps the drop key attached to this transaction when React moves
    // its focused handle. Ignore the keydown that activated this sensor.
    this.document.addEventListener("keydown", this.onKeyDown, true);
    props.onStart({ x: 0, y: 0 });
  }

  private onKeyDown = (event: KeyboardEvent) => {
    if (event === this.props.event) return;
    if (!["ArrowUp", "ArrowDown", "Space", "Enter", "Escape", "Tab"].includes(event.code)) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.code === "ArrowUp" || event.code === "ArrowDown") {
      this.props.options.onStep(event.code === "ArrowUp" ? "up" : "down");
      requestAnimationFrame(() => {
        this.handle?.focus({ preventScroll: true });
        this.handle?.scrollIntoView({ block: "nearest", inline: "nearest" });
      });
      return;
    }
    this.document.removeEventListener("keydown", this.onKeyDown, true);
    if (event.code === "Escape") this.props.onCancel();
    else this.props.onEnd();
    requestAnimationFrame(() => this.handle?.focus({ preventScroll: true }));
  };
}
