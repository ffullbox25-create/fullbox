(function () {
  const buildContextUrl = (contextId) => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const encoded = encodeURIComponent(String(contextId || '').trim());
    return `${protocol}//${window.location.host}/ws/agent/context/${encoded}/`;
  };

  const createContextClient = (options) => {
    const onEvent = typeof options?.onEvent === 'function' ? options.onEvent : () => {};
    const onState = typeof options?.onState === 'function' ? options.onState : () => {};
    let socket = null;
    let contextId = '';
    let closedByUser = false;
    let reconnectTimer = null;
    let reconnectDelay = 1000;

    const clearReconnect = () => {
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const isConnected = () => socket && socket.readyState === WebSocket.OPEN;

    const close = () => {
      closedByUser = true;
      clearReconnect();
      if (socket) {
        socket.close();
        socket = null;
      }
      onState('closed');
    };

    const scheduleReconnect = () => {
      if (closedByUser || !contextId) {
        return;
      }
      clearReconnect();
      reconnectTimer = window.setTimeout(() => connect(contextId), reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    };

    const connect = (nextContextId) => {
      const normalized = String(nextContextId || '').trim();
      if (!normalized || !window.WebSocket) {
        close();
        return;
      }
      if (contextId === normalized && isConnected()) {
        return;
      }
      closedByUser = false;
      contextId = normalized;
      clearReconnect();
      if (socket) {
        socket.close();
      }
      socket = new WebSocket(buildContextUrl(contextId));
      onState('connecting');
      socket.addEventListener('open', () => {
        reconnectDelay = 1000;
        onState('open');
      });
      socket.addEventListener('message', (event) => {
        let message = null;
        try {
          message = JSON.parse(event.data || '{}');
        } catch (err) {
          return;
        }
        if (message && message.type === 'agent.event' && message.event) {
          onEvent(message.event);
        }
      });
      socket.addEventListener('close', () => {
        onState('closed');
        scheduleReconnect();
      });
      socket.addEventListener('error', () => {
        onState('error');
      });
    };

    return {
      connect,
      close,
      isConnected,
    };
  };

  window.FullboxAgentRealtime = {
    createContextClient,
  };
})();
