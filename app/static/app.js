(() => {
  const room = document.querySelector("[data-room]");
  if (!room) return;
  const id = room.dataset.room;
  const scrollToLatest = () => {
    room.scrollTop = room.scrollHeight;
  };

  const closeReactionMenus = () => {
    room.querySelectorAll(".reaction-menu").forEach((menu) => {
      menu.hidden = true;
      menu.parentElement.querySelector("[data-message-toggle]").ariaExpanded = "false";
    });
  };

  const reactionButton = (messageId, value, glyph) => {
    const form = document.createElement("form");
    const button = document.createElement("button");
    form.method = "post";
    form.action = `/messages/${messageId}/reaction`;
    button.className = "reaction-button";
    button.name = "value";
    button.value = value;
    button.dataset.reactionValue = value;
    button.ariaLabel = value === "up" ? "thumbs up" : "thumbs down";
    button.title = button.ariaLabel;
    button.textContent = glyph;
    form.append(button);
    return form;
  };

  const syncReactions = (item, reactions = []) => {
    let summaries = item.querySelector(":scope > .message-row .reaction-summaries");
    if (!summaries) {
      summaries = document.createElement("span");
      summaries.className = "reaction-summaries";
      item.querySelector(":scope > .message-row .message-line").append(summaries);
    }
    summaries.replaceChildren();
    [
      ["up", "👍", "thumbs up"],
      ["down", "👎", "thumbs down"],
    ].forEach(([value, glyph, label]) => {
      const matching = reactions.filter((reaction) => reaction.value === value);
      if (!matching.length) return;
      const names = matching.map((reaction) => reaction.agent);
      const summary = document.createElement("span");
      summary.className = "reaction-summary";
      summary.title = `${glyph} ${names.join(", ")}`;
      summary.ariaLabel = `${label} by ${names.join(", ")}`;
      summary.textContent = `${glyph}${matching.length > 1 ? ` ${matching.length}` : ""}`;
      summaries.append(summary);
    });
    item.querySelectorAll(":scope > .reaction-menu [data-reaction-value]").forEach((button) => {
      button.classList.toggle(
        "selected",
        reactions.some(
          (reaction) => reaction.mine && reaction.value === button.dataset.reactionValue,
        ),
      );
    });
  };

  const createThreadPanel = (messageId) => {
    const panel = document.createElement("div");
    const replies = document.createElement("div");
    const form = document.createElement("form");
    const parent = document.createElement("input");
    const textarea = document.createElement("textarea");
    const send = document.createElement("button");
    panel.className = "thread-panel";
    panel.id = `thread-${messageId}`;
    panel.dataset.threadRoot = messageId;
    panel.hidden = true;
    replies.className = "thread-replies";
    form.className = "thread-compose";
    form.method = "post";
    form.action = `/rooms/${id}/messages`;
    parent.type = "hidden";
    parent.name = "parent_message_id";
    parent.value = messageId;
    textarea.name = "body";
    textarea.rows = 2;
    textarea.required = true;
    textarea.placeholder = "reply";
    textarea.ariaLabel = "reply";
    send.type = "submit";
    send.textContent = "send";
    form.append(parent, textarea, send);
    panel.append(replies, form);
    return panel;
  };

  const render = (message, reply = false) => {
    const item = document.createElement("div");
    const row = document.createElement("div");
    const line = document.createElement("button");
    const sender = document.createElement("span");
    const menu = document.createElement("div");
    item.className = reply ? "msg thread-reply" : "msg";
    item.dataset.messageId = message.id;
    row.className = "message-row";
    line.className = "message-line";
    line.type = "button";
    line.dataset.messageToggle = "";
    line.ariaExpanded = "false";
    sender.className = "who";
    sender.textContent = message.sender;
    line.append(sender, document.createTextNode(` ${message.body}`));
    row.append(line);
    if (!reply) {
      const thread = document.createElement("button");
      thread.className = "thread-toggle";
      thread.type = "button";
      thread.dataset.threadToggle = "";
      thread.ariaExpanded = "false";
      thread.textContent = "[reply]";
      row.append(thread);
    }
    menu.className = "reaction-menu";
    menu.hidden = true;
    menu.append(
      reactionButton(message.id, "up", "👍"),
      reactionButton(message.id, "down", "👎"),
    );
    item.append(row, menu);
    if (!reply) item.append(createThreadPanel(message.id));
    syncReactions(item, message.reactions);
    return item;
  };

  const syncThreadCount = (root) => {
    const count = root.querySelectorAll(":scope > .thread-panel .thread-reply").length;
    const toggle = root.querySelector(":scope > .message-row [data-thread-toggle]");
    if (toggle) toggle.textContent = count ? `[${count} ${count === 1 ? "reply" : "replies"}]` : "[reply]";
  };

  room.addEventListener("click", (event) => {
    const threadToggle = event.target.closest("[data-thread-toggle]");
    if (threadToggle) {
      const root = threadToggle.closest(".msg");
      const panel = root.querySelector(":scope > .thread-panel");
      panel.hidden = !panel.hidden;
      threadToggle.ariaExpanded = String(!panel.hidden);
      if (!panel.hidden) panel.querySelector("textarea").focus();
      return;
    }
    const toggle = event.target.closest("[data-message-toggle]");
    if (!toggle) return;
    const item = toggle.closest(".msg");
    const menu = item.querySelector(":scope > .reaction-menu");
    const willOpen = menu.hidden;
    closeReactionMenus();
    menu.hidden = !willOpen;
    toggle.ariaExpanded = String(willOpen);
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".msg")) closeReactionMenus();
  });

  if (window.location.hash.startsWith("#thread-")) {
    const panel = document.querySelector(window.location.hash);
    if (panel?.matches(".thread-panel")) {
      panel.hidden = false;
      const toggle = panel.parentElement.querySelector(":scope > .message-row [data-thread-toggle]");
      if (toggle) toggle.ariaExpanded = "true";
    }
  } else {
    requestAnimationFrame(scrollToLatest);
  }

  window.setInterval(async () => {
    try {
      const keepLatestVisible = room.scrollHeight - room.scrollTop - room.clientHeight < 48;
      const response = await fetch(`/api/rooms/${id}/messages`);
      if (!response.ok) return;
      const payload = await response.json();
      const known = new Map(
        [...room.querySelectorAll("[data-message-id]")].map((node) => [node.dataset.messageId, node]),
      );
      payload.messages.forEach((message) => {
        const item = known.get(message.id);
        if (item) {
          syncReactions(item, message.reactions);
          return;
        }
        if (message.parent_message_id) {
          const root = known.get(message.parent_message_id);
          if (!root) return;
          const reply = render(message, true);
          root.querySelector(":scope > .thread-panel .thread-replies").append(reply);
          known.set(message.id, reply);
          syncThreadCount(root);
          return;
        }
        const root = render(message);
        room.append(root);
        known.set(message.id, root);
      });
      if (keepLatestVisible) scrollToLatest();
    } catch {
      // A local server restart is expected to interrupt an occasional poll.
    }
  }, 4000);
})();
