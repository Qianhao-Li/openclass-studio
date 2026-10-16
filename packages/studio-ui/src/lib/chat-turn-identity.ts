import type { ChatRequestPayload } from "../types";

type UuidFactory = () => string;

export type IdentifiedTextChatRequestPayload = ChatRequestPayload & {
  session_id: string;
  turn_id: string;
  input_event_id: string;
  channel: "text";
  input_kind: "typed";
};

function defaultUuidFactory() {
  return crypto.randomUUID();
}

function createIdentityId(prefix: string, createUuid: UuidFactory) {
  return `${prefix}_${createUuid()}`;
}

export function createTextChatSessionId(createUuid: UuidFactory = defaultUuidFactory) {
  return createIdentityId("text-session", createUuid);
}

export function freezeTextChatTurnIdentity(
  payload: ChatRequestPayload,
  sessionId: string,
  createUuid: UuidFactory = defaultUuidFactory
): IdentifiedTextChatRequestPayload {
  const stableSessionId = payload.session_id?.trim() || sessionId.trim();
  if (!stableSessionId) {
    throw new Error("Text chat session id is required");
  }
  return {
    ...payload,
    session_id: stableSessionId,
    turn_id: payload.turn_id?.trim() || createIdentityId("turn", createUuid),
    input_event_id: payload.input_event_id?.trim() || createIdentityId("input-event", createUuid),
    channel: "text",
    input_kind: "typed",
  };
}
