import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPanel } from "./App";
import type { Chat } from "./types";

const scrollIntoView = vi.fn();

function makeChat(messageCount: number): Chat {
  return {
    chat_id: "chat_scroll_test",
    tenant_id: "tenant_test",
    user_id: "user_test",
    title: "Scroll behavior",
    created_at: "2026-08-09T00:00:00Z",
    updated_at: "2026-08-09T00:00:00Z",
    messages: Array.from({ length: messageCount }, (_, index) => ({
      message_id: `message_${index}`,
      chat_id: "chat_scroll_test",
      role: index % 2 === 0 ? "user" : "assistant",
      content: `Message ${index}`,
      mode: "chat",
      metadata: {},
      created_at: "2026-08-09T00:00:00Z",
    })),
  };
}

describe("ChatPanel scrolling", () => {
  beforeEach(() => {
    scrollIntoView.mockReset();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("keeps the reader's position until the three-dot button is clicked", () => {
    const props = {
      tasks: [],
      codeRuns: [],
      sending: false,
      onSend: vi.fn().mockResolvedValue(undefined),
      onOpenTask: vi.fn(),
      onOpenWorkspace: vi.fn(),
    };
    const { rerender } = render(<ChatPanel chat={makeChat(8)} {...props} />);
    const messages = screen.getByTestId("chat-messages");
    Object.defineProperties(messages, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 300 },
      scrollTop: { configurable: true, value: 100, writable: true },
    });

    fireEvent.scroll(messages);
    expect(screen.getByRole("button", { name: "跳到最新消息" })).toBeInTheDocument();

    scrollIntoView.mockClear();
    rerender(<ChatPanel chat={makeChat(9)} {...props} />);
    expect(scrollIntoView).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "跳到最新消息" }));
    expect(scrollIntoView).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "跳到最新消息" })).not.toBeInTheDocument();
  });

  it("shows streamed assistant text instead of a second typing indicator", () => {
    const chat = makeChat(2);
    chat.messages[1].metadata = { streaming: true };
    const { container } = render(
      <ChatPanel
        chat={chat}
        tasks={[]}
        codeRuns={[]}
        sending
        onSend={vi.fn().mockResolvedValue(undefined)}
        onOpenTask={vi.fn()}
        onOpenWorkspace={vi.fn()}
      />,
    );

    expect(container.querySelector(".chat-message.streaming")).toBeInTheDocument();
    expect(container.querySelector(".typing-indicator")).not.toBeInTheDocument();
  });

  it("shows reasoning while thinking and collapses it when the answer starts", () => {
    const props = {
      tasks: [],
      codeRuns: [],
      sending: true,
      onSend: vi.fn().mockResolvedValue(undefined),
      onOpenTask: vi.fn(),
      onOpenWorkspace: vi.fn(),
    };
    const chat = makeChat(2);
    chat.messages[1].content = "";
    chat.messages[1].metadata = {
      streaming: true,
      thinking_enabled: true,
      reasoning: "先识别问题，再组织答案。",
    };

    const { rerender } = render(<ChatPanel chat={chat} {...props} />);
    const details = screen.getByText("思考过程").closest("details");
    expect(details).toHaveAttribute("open");
    expect(screen.getByText("先识别问题，再组织答案。")).toBeVisible();

    const answeringChat = {
      ...chat,
      messages: chat.messages.map((message) => (
        message.message_id === chat.messages[1].message_id
          ? { ...message, content: "这是最终答案。" }
          : message
      )),
    };
    rerender(<ChatPanel chat={answeringChat} {...props} />);
    expect(details).not.toHaveAttribute("open");

    fireEvent.click(screen.getByText("思考过程"));
    expect(details).toHaveAttribute("open");
  });

  it("lets the user enable thinking for a chat message", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatPanel
        chat={makeChat(0)}
        tasks={[]}
        codeRuns={[]}
        sending={false}
        onSend={onSend}
        onOpenTask={vi.fn()}
        onOpenWorkspace={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "深度思考" }));
    expect(screen.getByRole("button", { name: "深度思考" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.change(screen.getByPlaceholderText(/输入问题/), {
      target: { value: "请认真分析这个问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(onSend).toHaveBeenCalledWith("请认真分析这个问题", "auto", true);
  });
});
