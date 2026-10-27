//! Пробник трёх правок к узлу Axiome.
//!
//! Нужен затем, что о правках нельзя судить по исходнику: набор
//! возможностей, разрешающий список запросов и права модульного счёта
//! проверяются на исполнении, а не на чтении. Пробник трогает ровно те
//! места и больше ничего.
//!
//! * Загрузка. Контракт объявляет `requires_stargate` — возможность
//!   `stargate` объявлена зависимостью `cosmwasm-std`. На неисправленном
//!   узле он не загрузится вовсе, то есть **первая правка проверяется
//!   самим фактом загрузки**.
//! * Запрос. `QueryMsg::Referral` зовёт `x/referral` через Stargate и
//!   отдаёт ответ цепочки **сырым**. Разбирать его здесь нельзя: тогда
//!   в замере был бы наш разбор, а не ответ модуля.
//! * Сжигание. `ExecuteMsg::Burn` сжигает то, что пришло со сделкой.
//!   Без модульного счёта `wasm` с правом `burner` узел отвечает на это
//!   паникой `module account wasm does not exist`.

use cosmwasm_schema::cw_serde;
use cosmwasm_std::{
    entry_point, to_json_vec, BankMsg, Binary, ContractResult, Deps, DepsMut, Empty, Env,
    MessageInfo, QueryRequest, Response, StdError, StdResult, SystemResult,
};

/// Путь запроса из `proto/axiome/referral/v1beta1/query.proto`.
/// Ровно он и просится в разрешающий список на стороне узла.
const REFERRAL_GET: &str = "/axiome.referral.v1beta1.Query/Get";

#[cw_serde]
pub struct InstantiateMsg {}

#[cw_serde]
pub enum ExecuteMsg {
    /// Сжечь всё, что пришло со сделкой.
    Burn {},
}

#[cw_serde]
pub enum QueryMsg {
    /// Спросить `x/referral` про адрес и отдать ответ цепочки как есть.
    Referral { address: String },
}

#[entry_point]
pub fn instantiate(
    _deps: DepsMut,
    _env: Env,
    _info: MessageInfo,
    _msg: InstantiateMsg,
) -> StdResult<Response> {
    Ok(Response::new().add_attribute("probe", "stargate+burn"))
}

#[entry_point]
pub fn execute(
    _deps: DepsMut,
    _env: Env,
    info: MessageInfo,
    msg: ExecuteMsg,
) -> StdResult<Response> {
    match msg {
        ExecuteMsg::Burn {} => {
            if info.funds.is_empty() {
                return Err(StdError::generic_err("нечего сжигать: монет не пришло"));
            }
            let what = info
                .funds
                .iter()
                .map(|c| c.to_string())
                .collect::<Vec<_>>()
                .join(",");
            Ok(Response::new()
                .add_message(BankMsg::Burn {
                    amount: info.funds,
                })
                .add_attribute("burned", what))
        }
    }
}

#[entry_point]
pub fn query(deps: Deps, _env: Env, msg: QueryMsg) -> StdResult<Binary> {
    match msg {
        QueryMsg::Referral { address } => {
            let request: QueryRequest<Empty> = QueryRequest::Stargate {
                path: REFERRAL_GET.to_string(),
                data: Binary::from(encode_get_request(&address)),
            };
            // Сырым запросом, а не `querier.query`: тот разобрал бы
            // ответ по нашей структуре, и в замере оказалось бы наше
            // представление вместо ответа модуля.
            let raw = to_json_vec(&request)?;
            match deps.querier.raw_query(&raw) {
                SystemResult::Err(e) => Err(StdError::generic_err(format!("узел: {e}"))),
                SystemResult::Ok(ContractResult::Err(e)) => {
                    Err(StdError::generic_err(format!("цепочка отказала: {e}")))
                }
                SystemResult::Ok(ContractResult::Ok(value)) => Ok(value),
            }
        }
    }
}

/// `GetRequest { string acc_address = 1; }` в protobuf.
///
/// Руками, а не через кодогенерацию: одно строковое поле, и тянуть
/// `prost` в контракт ради него значило бы ещё одну зависимость в том
/// самом артефакте, который идёт в цепочку.
fn encode_get_request(address: &str) -> Vec<u8> {
    let body = address.as_bytes();
    let mut out = Vec::with_capacity(body.len() + 3);
    out.push(0x0a); // поле 1, тип 2 (строка)
    let mut left = body.len();
    loop {
        let mut byte = (left & 0x7f) as u8;
        left >>= 7;
        if left != 0 {
            byte |= 0x80;
        }
        out.push(byte);
        if left == 0 {
            break;
        }
    }
    out.extend_from_slice(body);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zapros_koditsya_kak_protobuf() {
        // Короткий адрес: тег, длина одним байтом, тело.
        let a = "axm1v6xdm93m5s9lvu0ux4k76s2p7hgkzj00u8w5z7";
        let got = encode_get_request(a);
        assert_eq!(got[0], 0x0a);
        assert_eq!(got[1] as usize, a.len());
        assert_eq!(&got[2..], a.as_bytes());
        // Длина больше 127 требует двух байтов varint — проверяем, что
        // кодировщик не врёт на границе, хотя адреса столько не бывают.
        let long = "x".repeat(130);
        let got = encode_get_request(&long);
        assert_eq!(&got[0..3], &[0x0a, 0x82, 0x01]);
        assert_eq!(got.len(), 3 + 130);
    }
}
