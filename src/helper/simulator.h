// SPDX-License-Identifier: GPL-3.0-or-later
// Simulate(): what installing / updating / removing an app would do (docs/design.md section 5).
#pragma once

#include "catalog.h"
#include "config.h"

#include <QString>

#include <functional>

namespace AginoStore {

class PkClient;

// done(ok, errorCode, text): text is the JSON document when ok, otherwise an error message.
using SimulateDone = std::function<void(bool ok, const QString &errorCode, const QString &text)>;

void simulateAction(PkClient *pk, const HelperConfig &config, const AppSummary &app, const QString &action,
                    SimulateDone done);

} // namespace AginoStore
