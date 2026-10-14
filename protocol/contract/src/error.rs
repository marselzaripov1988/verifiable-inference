use cosmwasm_std::StdError;
use thiserror::Error;

#[derive(Error, Debug, PartialEq)]
pub enum ContractError {
    #[error("{0}")]
    Std(#[from] StdError),

    #[error("нужны права администратора")]
    NotAdmin,

    #[error("нужны права attestor")]
    NotAttestor,

    #[error("исполнитель не зарегистрирован")]
    ExecutorUnknown,

    #[error("исполнитель не допущен к заданиям (статус {status})")]
    ExecutorNotActive { status: String },

    #[error("исполнитель уже зарегистрирован")]
    ExecutorExists,

    #[error("забаненного исполнителя вернуть нельзя")]
    ExecutorBanned,

    #[error("связка {model}/{runtime}/{dtype} не та, для которой измерен порог")]
    WrongPinning {
        model: String,
        runtime: String,
        dtype: String,
    },

    #[error("задание не найдено")]
    JobUnknown,

    #[error("задание в состоянии {state}, ожидалось {expected}")]
    WrongJobState { state: String, expected: String },

    #[error("это не ваше задание")]
    NotYourJob,

    #[error("ответ {got} токенов, нужно от {need}: ниже этой длины способ не работает")]
    AnswerTooShort { got: u32, need: u32 },

    #[error("исполнитель не может проверять своё задание")]
    SelfCheck,

    #[error("этот проверяющий уже сдал проверку по заданию")]
    DuplicateCheck,

    #[error("собрано {got} проверок из {need}")]
    NotEnoughChecks { got: u32, need: u32 },

    #[error("проверок уже достаточно")]
    ChecksComplete,

    #[error("checks_required должно быть больше нуля")]
    ZeroChecks,

    #[error("раскрытый вектор не совпал с обязательством: хэш {got}, ожидался {want}")]
    RevealMismatch { got: String, want: String },

    #[error("проверка от {checker} по заданию не найдена")]
    CheckUnknown { checker: String },

    #[error("пустой вектор вероятностей")]
    EmptyVector,

    #[error("вердикт уже вынесен, спор разбирается до него")]
    AlreadySettled,

    #[error("залог вносится в {want}, пришло {got}")]
    WrongStakeDenom { want: String, got: String },

    #[error("залог {have}, нужно не меньше {need}")]
    StakeTooSmall { have: String, need: String },

    #[error("залог нельзя забрать при {open} незакрытых заданиях")]
    StakeLocked { open: u64 },

    #[error("в залоге {have}, забрать {want} нельзя")]
    StakeInsufficient { have: String, want: String },

    #[error("расчёты принимаются только от {want}, пришло от {got}")]
    WrongCw20 { want: String, got: String },

    #[error("цена задания нулевая")]
    ZeroPrice,

    #[error("по заданию уже выплачено")]
    AlreadyPaid,

    #[error("доля проверяющих {bps} bps — больше 10000 не бывает")]
    BadFeeBps { bps: u16 },

    #[error("проба на эталонном векторе не пройдена: расхождение {got}, предел {limit}")]
    ConformanceFailed { got: u64, limit: u64 },

    #[error("проба на эталонном векторе не пройдена вовсе")]
    ConformanceMissing,

    #[error("проба снята против эталона версии {have}, сейчас {want}")]
    ConformanceStale { have: u64, want: u64 },

    #[error("в пробе {got} чисел, эталон требует {want}")]
    ConformanceShape { got: u64, want: u64 },

    #[error("эталонный набор пуст: пробу не из чего составить")]
    EmptyProbe,
}
