// SPDX-License-Identifier: GPL-3.0-or-later
pragma Singleton
import QtQuick

// Page navigation state shared by all pages (pages: home, category, detail, installed, tasks,
// settings). A non-empty query shows the search results instead of the page.
QtObject {
    id: router

    property string page: "home"
    property string category: ""
    property string appId: ""
    property string query: ""
    property var history: []

    readonly property bool canGoBack: history.length > 0

    function snapshot() {
        return { "page": page, "category": category, "appId": appId }
    }

    function go(newPage, params) {
        const p = params || {}
        if (query === "" && (page !== newPage || newPage === "detail" || newPage === "category"))
            history = history.concat([snapshot()])
        query = ""
        category = p.category !== undefined ? p.category : category
        appId = p.appId !== undefined ? p.appId : appId
        page = newPage
    }

    function openApp(id) { go("detail", { "appId": id }) }
    function openCategory(id) { go("category", { "category": id }) }

    function back() {
        if (history.length === 0)
            return
        const last = history[history.length - 1]
        history = history.slice(0, history.length - 1)
        query = ""
        category = last.category
        appId = last.appId
        page = last.page
    }
}
