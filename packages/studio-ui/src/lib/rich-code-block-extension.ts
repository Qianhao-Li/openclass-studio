import CodeBlock from "@tiptap/extension-code-block";
import { ReactNodeViewRenderer } from "@tiptap/react";

import { RichCodeBlockView } from "../components/course-studio/rich-code-block-view";

export const RichCodeBlock = CodeBlock.extend({
  addNodeView() {
    return ReactNodeViewRenderer(RichCodeBlockView);
  },
});
