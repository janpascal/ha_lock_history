
const LitElement = Object.getPrototypeOf(
  customElements.get("ha-panel-lovelace")
);
const html = LitElement.prototype.html;
const css = LitElement.prototype.css;

var imported_modules = {
   "paper-item": ["paper-item", "paper-item-body"], 
};

for (var module in imported_modules) {
    var elements = imported_modules[module];
    for (var i = 0; i < elements.length; i++) {
	var element = elements[i];
	if (!customElements.get(element)) {
	  console.log("imported", element);
	  import("https://unpkg.com/@polymer/" + module + "/" + element + ".js?module");
	}
    }
}

class LockHistory extends LitElement {
  static get properties() {
    return {
      hass: {},
      isWide: {},
      _config: {},
      _historyItems: {},
    };
  }

  get _name() {
    return this._config.name || "";
  }

  renderStyle() {
    return html`
      <style>
        paper-toggle-button {
          padding-top: 16px;
        }
        paper-item-body [secondary] {
            font-size: x-small;
        }
        ha-card.lock_history {
            overflow-y: auto;
            height: 400px;
        }
      </style>
    `;
  }

  render() {
    if (
      !this.hass ||
      this._historyItems === undefined
    ) {
      return html`
        <hass-loading-screen></hass-loading-screen>
      `;
    }

    return html`
      ${this.renderStyle()}
      <ha-card header="Lock History" class="lock_history">
            ${this._historyItems.map((entry) => {
              return html`
                <paper-icon-item .entry=${entry}>
                  <ha-icon 
                      icon=${entry.state == "Home" ? "mdi:home" : "mdi:lock"} 
                      slot="item-icon">
                  </ha-icon>
                  <paper-item-body two-line>
                    <div>${entry.name}</div>
                    <div secondary>${entry.date}</div>
                  </paper-item-body>
                </paper-item>
              `;
            })}
      </ha-card>
    `;
  }

  getCardSize() {
    return 3;
  }

  setConfig(config) {
    this._config = config;
  }

  firstUpdated(changedProps) {
    super.firstUpdated(changedProps);
    this._fetchData();
    function history_event_fired(conn, eventData) {
      // console.log("history event fired");
      this._fetchData();
    }

    this.hass.connection.addEventListener("lock_history.history_updated", history_event_fired);
  }

  async _fetchData() {
    const data = await this.hass.callWS({ type: "lock_history/history" });
    this._historyItems = data.history;
  }

}

customElements.define('user-code', LockHistory);

