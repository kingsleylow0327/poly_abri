from dataclasses import dataclass, asdict

@dataclass
class OrderDto:
    token_id: str   
    price: float
    size: float
    
    def __post_init__(self):
        self.price = round(float(self.price), 2)
    
    def to_dict(self) -> dict:
        return asdict(self)