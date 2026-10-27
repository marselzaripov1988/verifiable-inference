#!/usr/bin/env python3
"""Три правки к axm-node v2.2.1, которые просим у площадки.

Ровно то, о чём письмо, и ничего больше: возможность stargate, список
разрешённых запросов вместо отказа, модульный счёт wasm с правом burner.
Скрипт идемпотентен: повторный запуск ничего не делает.
"""
import pathlib
import sys

SRC = pathlib.Path(sys.argv[1])


def edit(path, old, new, what):
    p = SRC / path
    t = p.read_text(encoding="utf-8")
    if new in t:
        print(f"  уже есть: {what}")
        return
    if t.count(old) != 1:
        raise SystemExit(f"ОТКАЗ: в {path} якорь встречается "
                         f"{t.count(old)} раз, ждали один: {what}")
    p.write_text(t.replace(old, new), encoding="utf-8")
    print(f"  правка: {what} ({path})")


print("1. возможность stargate")
edit("x/wasm/module.go",
     '"iterator,staking,distribution,cosmwasm_1_1,cosmwasm_1_2,cosmwasm_1_3,cosmwasm_1_4"',
     '"iterator,staking,distribution,stargate,cosmwasm_1_1,cosmwasm_1_2,cosmwasm_1_3,cosmwasm_1_4"',
     "stargate в набор возможностей")

print("2. разрешающий список запросов вместо RejectStargateQuerier")
# Маршрутизатор запросов depinject НЕ выдаёт: в runtime.ProvideApp
# (cosmos-sdk v0.50.3) среди выходов только *baseapp.MsgServiceRouter.
# Поэтому держим указатель, который app.go заполняет сразу после
# appBuilder.Build, когда маршрутизатор уже есть. Запрос из контракта
# приходит много позже, на исполнении сделки.
edit("x/wasm/module.go",
     '\texportedtypes "github.com/axiome-pro/axm-node/x/wasm/types"',
     '\treferraltypes "github.com/axiome-pro/axm-node/x/referral/types"\n'
     '\texportedtypes "github.com/axiome-pro/axm-node/x/wasm/types"',
     "импорт типов referral")

edit("x/wasm/module.go",
     "func init() {",
     '''// stargateRouter — маршрутизатор запросов, выданный позже сборки.
//
// AcceptListStargateQuerier требует GRPCQueryRouter в момент создания
// кипера, а depinject его не выдаёт. Пустой указатель до Build даёт
// отказ на запрос, а не панику.
type stargateRouter struct {
\tr *baseapp.GRPCQueryRouter
}

func (s *stargateRouter) Route(path string) baseapp.GRPCQueryHandler {
\tif s == nil || s.r == nil {
\t\treturn nil
\t}
\treturn s.r.Route(path)
}

// StargateQueryRouter заполняется в app.go сразу после appBuilder.Build.
var StargateQueryRouter = &stargateRouter{}

// SetStargateQueryRouter — вызывать один раз, после сборки приложения.
func SetStargateQueryRouter(r *baseapp.GRPCQueryRouter) {
\tStargateQueryRouter.r = r
}

// StargateAcceptList — какие запросы контракту разрешены. Тип ответа
// закреплён нарочно: по нему ответ разбирается и подаётся контракту как
// JSON, и подменить его при следующем выпуске модуля нельзя незаметно.
var StargateAcceptList = wasmkeeper.AcceptedStargateQueries{
\t"/axiome.referral.v1beta1.Query/Get": &referraltypes.GetResponse{},
}

func init() {''',
     "ленивый маршрутизатор и список разрешённых запросов")

edit("x/wasm/module.go",
     "\t\t\tauthority,\n\t\t)",
     "\t\t\tauthority,\n"
     "\t\t\twasmkeeper.WithQueryPlugins(&wasmkeeper.QueryPlugins{\n"
     "\t\t\t\tStargate: wasmkeeper.AcceptListStargateQuerier(\n"
     "\t\t\t\t\tStargateAcceptList, StargateQueryRouter, in.Cdc),\n"
     "\t\t\t}),\n\t\t)",
     "QueryPlugins в NewKeeper")

edit("app/app.go",
     '\t_ "github.com/axiome-pro/axm-node/x/wasm"',
     '\taxmwasm "github.com/axiome-pro/axm-node/x/wasm"',
     "именованный импорт x/wasm")

edit("app/app.go",
     "\tapp.App = appBuilder.Build(db, traceStore, baseAppOptions...)",
     "\tapp.App = appBuilder.Build(db, traceStore, baseAppOptions...)\n\n"
     "\t// Маршрутизатор запросов для Stargate: до Build его не существует.\n"
     "\taxmwasm.SetStargateQueryRouter(app.GRPCQueryRouter())",
     "выдача маршрутизатора после Build")

print("3. модульный счёт wasm с правом burner")
edit("app/app.yaml",
     "        - account: vote",
     "        - account: wasm\n"
     "          permissions: [ burner ]\n"
     "        - account: vote",
     "wasm: [burner] в module_account_permissions")

print("готово")
